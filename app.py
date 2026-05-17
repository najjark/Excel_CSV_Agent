import sys
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import google.genai as genai
from google.genai import types
import pandas as pd
import io
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import os
import numpy as np
import re
import datetime
from datetime import timedelta
import time
import ast
from werkzeug.utils import secure_filename
import threading
import uuid
import pathlib
import shutil
from dotenv import load_dotenv
load_dotenv()
from flask import session
from flask_session import Session
class TimeoutException(Exception):
    pass

app = Flask(__name__)

# Define constants
UPLOAD_FOLDER = "stored_dataframes"
SESSION_FOLDER = "flask_session"

# file paths
pathlib.Path(UPLOAD_FOLDER).mkdir(exist_ok=True)
pathlib.Path(SESSION_FOLDER).mkdir(exist_ok=True)

# Define the cleanup logic
def power_wash_storage():
    folders_to_clean = [UPLOAD_FOLDER, SESSION_FOLDER]
    for folder in folders_to_clean:
        try:
            if os.path.exists(folder):
                shutil.rmtree(folder)
            pathlib.Path(folder).mkdir(exist_ok=True)
        except Exception as e:
            print(f"⚠️ Note: Could not clear {folder}: {e}")

# Configure Flask
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY")
app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_FILE_DIR"] = SESSION_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=2)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("FLASK_ENV") == "production"

allowed_origins = os.environ.get("ALLOWED_ORIGINS", "http://localhost:5000").split(",")
CORS(app, origins=allowed_origins, supports_credentials=True)

Session(app)

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["10 per minute"]
)


@app.errorhandler(429)
def rate_limit_exceeded(e):
    return jsonify({"error": "Too many requests. Please wait a moment and try again."}), 429


def get_dataframes():
    """Retrieves DataFrames from disk based on the current session."""
    dataframes = {}
    file_map = session.get("file_map", {})

    for name, path in file_map.items():
        if os.path.exists(path):
            # Read the parquet file back into memory
            dataframes[name] = pd.read_parquet(path)
            
    return dataframes

def validate_code(code):
    tree = ast.parse(code)

    forbidden_nodes = (
        ast.Import,
        ast.ImportFrom,
        ast.With,
        ast.ClassDef,
        ast.FunctionDef,
        ast.AsyncFunctionDef,
        ast.Global,
        ast.Nonlocal
    )
    
    forbidden_names = {
        "exec", "eval", "open", "compile", "__import__",
        "input", "globals", "locals", "vars", 
        "getattr", "setattr", "delattr"
    }

    forbidden_attrs = {
        "mro",
        "subclasses",
        "gi_frame",
        "gi_code",
        "f_locals",
        "f_globals",
        "__class__",
        "__bases__",
        "__dict__"
    }
    
    for node in ast.walk(tree):
        if isinstance(node, forbidden_nodes):
            raise ValueError("Forbidden Python construct detected")

        if isinstance(node, ast.Name):
            if node.id in forbidden_names:
                raise ValueError(f"Forbidden name used: {node.id}")

        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__") or node.attr in forbidden_attrs:
                raise ValueError(f"Forbidden attribute access: {node.attr}")

def execute_with_timeout(code, dataframes, timeout_seconds=30):
    result_container = {"result": None, "error": None}
    
    def run():
        try:
            import pandas as pd
            import numpy as np
            from RestrictedPython import compile_restricted, safe_builtins, utility_builtins
            from RestrictedPython.Guards import guarded_iter_unpack_sequence
            from RestrictedPython.Eval import default_guarded_getitem, default_guarded_getiter

            for func in ['read_csv', 'read_excel', 'read_json', 'read_sql', 'read_pickle', 'to_csv', 'to_excel']:
                setattr(pd, func, None)

            builtins = safe_builtins.copy()
            builtins.update(utility_builtins)
            
            safe_globals = {
                "__builtins__": builtins,
                "pd": pd,
                "np": np,
                "_getitem_": default_guarded_getitem,
                "_getiter_": default_guarded_getiter,
                "_write_": lambda x: x,
                "_iter_unpack_sequence_": guarded_iter_unpack_sequence,
                **dataframes
            }
            
            compiled = compile_restricted(code, '<string>', 'exec')
            exec(compiled, safe_globals)
            
            raw_result = safe_globals.get("result", None)
            
            if isinstance(raw_result, pd.DataFrame):
                raw_result = raw_result.head(100).to_dict(orient="records")

            result_container["result"] = raw_result

        except Exception as e:
            result_container["error"] = str(e)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout_seconds)
    
    if thread.is_alive():
        raise TimeoutException("Code execution timed out.")
    
    if result_container["error"]:
        raise RuntimeError(result_container["error"])
    
    return result_container["result"]


def get_df_info(dataframes):
    info = []
    for name, df in dataframes.items():
        # Clean up dtypes to be more readable
        types = {col: str(dtype) for col, dtype in df.dtypes.items()}
        
        # Grab a single row as a sample to show data format
        sample = df.head(1).to_dict(orient='records')
        
        # Compact string representation
        df_str = (
            f"Table: {name} ({len(df)} rows)\n"
            f"Schema: {types}\n"
            f"Sample: {sample}"
        )
        info.append(df_str)
    
    return "\n\n".join(info)

def ask_agent(question, dataframes, retries=2, delay=1):
    prompt = f"""
    You are a Python/Pandas code generator. Environment is sandboxed.

    DATA SAMPLE:
    {get_df_info(dataframes)}

    RULES:
    - If question is unrelated to the data, return only: INVALID_QUESTION
    - Output raw Python only. No markdown, no backticks, no explanation.
    - Assign final result to 'result'.
    - Read-only: no inplace=True. Use only 'pd', 'np', and the provided dataframe names.
    - CRITICAL: Before merging or joining on ID columns, ensure both columns are the same type (e.g., use .astype(str)) to avoid merge errors.
    QUESTION: {question}
    """
        
    for attempt in range(retries):
        try:
            response = client.models.generate_content(
                model="gemini-3.1-flash-lite",
                contents=prompt,
                config=types.GenerateContentConfig(
                    max_output_tokens=300,
                    temperature=0.3
                )
            )
            return response.text

        except Exception as e:
            err = str(e)
            if any(code in err for code in ["503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED"]):
                if attempt < retries - 1:
                    time.sleep(delay * (attempt + 1))
                    continue
            raise
    raise RuntimeError("Gemini API unavailable after multiple retries")

def execute_code(code, dataframes):
    if code.strip() == "INVALID_QUESTION":
        raise ValueError("Please ask a question related to your data.")

    validate_code(code)

    try:
        result = execute_with_timeout(code, dataframes, 30)

        if result is None:
            raise ValueError("Code ran but did not produce a result")

        return result
    except Exception as e:
        raise e

def cleanup_old_files(max_age_hours=1):
    if not os.path.exists(UPLOAD_FOLDER):
        pathlib.Path(UPLOAD_FOLDER).mkdir(exist_ok=True)
        return
    
    now = time.time()
    max_age = max_age_hours * 3600

    for filename in os.listdir(UPLOAD_FOLDER):
        path = os.path.join(UPLOAD_FOLDER, filename)

        try:
            if os.path.isfile(path):
                file_age = now - os.path.getmtime(path)
                if file_age > max_age:
                    os.remove(path)
        except Exception as e:
            print(f"Cleanup error: {e}")

@app.route("/upload", methods=["POST"])
@limiter.limit("10 per minute")
def upload():
    try:
        # Cleanup old files first
        cleanup_old_files()
        
        file = request.files.get("file")
        
        # Check if file exists first to avoid AttributeError
        if not file or file.filename == '':
            return jsonify({"error": "No file selected"}), 400
        
        # Check size
        file.seek(0, os.SEEK_END)
        size_in_bytes = file.tell()
        file.seek(0)
    
        if size_in_bytes > 5 * 1024 * 1024: # 5MB limit
            return jsonify({"error": "File size exceeds 5MB limit"}), 400

        # Session / User Setup
        if "user_id" not in session:
            session["user_id"] = str(uuid.uuid4())
        
        if len(session.get("file_map", {})) >= 5:
            return jsonify({"error": "Maximum of 5 files allowed"}), 400
        
        filename = secure_filename(file.filename)
        clean_name = re.sub(r'[^a-zA-Z0-9_]', '_', filename.split(".")[0])
        ext = filename.lower().split(".")[-1]
        
        # Processing
        if ext == "csv":
            # Peek for CSV
            df = pd.read_csv(io.BytesIO(file.read()))
        elif ext in ("xlsx", "xls"):
            # Excel must be read in full (with 5 MB limit)
            df = pd.read_excel(io.BytesIO(file.read()), engine = 'openpyxl')
        else:
            return jsonify({"error": "Unsupported file type"}), 400

        # Row/Column Validation (After loading)
        if len(df) > 10000 or len(df.columns) > 100:
            return jsonify({"error": "File exceeds row (10k) or column (100) limits"}), 400
        
        # Save as Parquet
        file_path = os.path.join(UPLOAD_FOLDER, f"{session['user_id']}_{clean_name}.parquet")
        df.to_parquet(file_path, index=False)        

        if "file_map" not in session:
            session["file_map"] = {}
        session["file_map"][clean_name] = file_path
        session.modified = True
        
        return jsonify({"name": clean_name, "rows": len(df), "cols": len(df.columns)})
    except Exception as e:
        print(f"Upload error: {e}")
        return jsonify({"error": "Upload failed. Please check your file and try again."}), 500

def make_serializable(obj):
    if isinstance(obj, (np.integer)):
        return int(obj)
    if isinstance(obj, (np.floating)):
        return round(float(obj), 4)
    if isinstance(obj, (pd.Timestamp, datetime.datetime)):
        return obj.isoformat()
    if isinstance(obj, pd.Period):
        return str(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, list):
        return [make_serializable(i) for i in obj]
    if isinstance(obj, dict):
        return {k: make_serializable(v) for k, v in obj.items()}
    if pd.isna(obj):
        return None
    return obj

@app.route("/ask", methods=["POST"])
@limiter.limit("5 per minute")
def ask():
    data = request.json
    if not data or "question" not in data:
        return jsonify({"error": "Missing question"}), 400

    question = data["question"]
    if len(question) > 500:
        return jsonify({"error": "Question too long"}), 400

    # Cleanup old files first
    cleanup_old_files()

    # Clean session + keep active files alive
    file_map = session.get("file_map", {})
    valid_paths = {}

    for name, path in file_map.items():
        if os.path.exists(path):
            os.utime(path, None)  # refresh last-used time
            valid_paths[name] = path

    session["file_map"] = valid_paths

    if valid_paths != file_map:
        session["file_map"] = valid_paths
        session.modified = True
    
    # Now load dataframes
    dataframes = get_dataframes()
    if not dataframes:
        return jsonify({"error": "No files uploaded. Please upload a file first."}), 400

    try:
        code = ask_agent(question, dataframes)
        result = execute_code(code, dataframes)

        # Detect which dataframes were used in the generated code
        used_files = []
        for name in dataframes.keys():
            if re.search(rf"\b{name}\b", code):
                used_files.append(name)
        
        if isinstance(result, list) and len(result) > 500:
            result = result[:500]
        
        result = make_serializable(result)
        
        # Size check
        if sys.getsizeof(str(result)) > 5 * 1024 * 1024: # 5MB limit
            return jsonify({"error": "Result is too large to display"}), 400
        
        elif isinstance(result, pd.Series):
            result = result.tolist()
        elif isinstance(result, np.ndarray):
            result = result.tolist()
                
        return jsonify({
            "code": code,
            "result": result,
            "used_files": used_files
        })
        
    # Custom Error Messages
    except ValueError as ve:
        return jsonify({"error": "Analysis failed, please try a simpler and check your files."}), 400
    
    except TimeoutException:
        return jsonify({"error": "Request timed out. Try a simpler question."}), 408

    except RuntimeError as re_err:
        # Log error to check in terminal
        print(f"Logging technical error: {re_err}") 

        # Check if API related
        if str(re_err) == "Gemini API unavailable after multiple retries":
            return jsonify({"error": "The AI service is currently busy. Please try again in a moment."}), 503
        
        # For all other code execution errors (like merge/type errors), send a safe message to the UI
        return jsonify({
            "error": "Analysis failed due to data inconsistency (e.g., mismatched column types). Please check your files."
        }), 500
    
    except Exception as e:
        print(f"Internal error: {e}")
        return jsonify({"error": "Something went wrong. Please try again."}), 500


@app.route("/remove", methods=["POST"])
def remove():
    try:
        data = request.json
        name = data.get("name")
        
        if not name:
            return jsonify({"error": "Missing name"}), 400
            
        file_map = session.get("file_map", {})

        if name in file_map:
            file_path = file_map[name]
            
            # Security Check: Ensure the file is actually inside our UPLOAD_FOLDER
            abs_storage_dir = os.path.abspath(UPLOAD_FOLDER)
            abs_file_path = os.path.abspath(file_path)
            
            if not abs_file_path.startswith(abs_storage_dir):
                return jsonify({"error": "Unauthorized file path"}), 403

            # Delete the physical file from disk
            if os.path.exists(file_path):
                os.remove(file_path)
            
            # Remove from session and save
            file_map.pop(name)
            session["file_map"] = file_map
            session.modified = True

        return jsonify({"success": True})
    except Exception as e:
        print(f"Remove error: {e}")
        return jsonify({"error": "Could not remove file. Please try again."}), 500


@app.route("/inspect", methods=["POST"])
def inspect():
    try:
        data = request.json
        name = data.get("name")
        file_map = session.get("file_map", {})

        if name not in file_map:
            return jsonify({"error": "File not found"}), 404

        file_path = file_map[name]
        
        # Read only the first 3 rows and the column names
        df = pd.read_parquet(file_path)
        
        # Format datetime (YYYY-MM-DD)
        for col in df.select_dtypes(include=['datetime64']).columns:
            df[col] = df[col].dt.strftime('%Y-%m-%d')
    
        # Prepare the preview data
        preview = df.head(3).to_dict(orient="records")
        columns = df.columns.tolist()
        
        # Make sure values are JSON-safe
        preview = make_serializable(preview)

        return jsonify({
            "name": name,
            "columns": columns,
            "dtypes": {col: str(dtype) for col, dtype in df.dtypes.items()},
            "preview": preview
        })
        
    except Exception as e:
        print(f"Inspection error: {e}")
        return jsonify({"error": "Could not read file preview"}), 500


@app.route("/script.js")
def serve_js():
    return send_from_directory(".", "script.js")

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

@app.route("/style.css")
def serve_css():
    return send_from_directory(".", "style.css")

@app.route("/favicon.ico")
def favicon():
    return send_from_directory(".", "favicon.ico")

if __name__ == "__main__":
    # Check if we are in the main process (not the reloader)
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        print("🚀 First boot: Cleaning up old session data...")
        power_wash_storage()
    
    app.run(host="127.0.0.1", port=5000, debug=True)