# Excel & CSV File Analysis Agent

## Overview
A web app that lets you query Excel and CSV files using plain English.
Powered by Google's Gemini model, it generates and executes Pandas code to answer
questions about your data.

## How It Works
1. Upload one or more Excel or CSV files
2. Ask questions about your data in plain English (e.g. "Which customer spent the most in March 2024?")
3. The agent generates and executes Pandas code to answer your question

## Setup
1. Clone the repository
2. Install dependencies: `pip install flask flask-cors google-genai pandas openpyxl`
3. Replace `YOUR_GEMINI_API_KEY` in `app.py` with your key — get one free at [Google AI Studio](https://aistudio.google.com)
4. Run: `python app.py`
5. Open `http://localhost:5000` in your browser

## Supported File Types
- Excel (.xlsx, .xls)
- CSV (.csv)