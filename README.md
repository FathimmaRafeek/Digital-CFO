# LucidPlus SME Digital CFO

A local demo dashboard for exploring small-business cash flow, expenses, receivables, payables, and CFO insights. The frontend is served by a FastAPI application; no separate frontend build is required.

## Features

- Financial cockpit with cash, expenses, runway, receivables, and payables summaries
- Cash-flow forecast and interactive cash-flow and expense charts
- Receivables and payables tracking, with transaction CSV upload
- CFO briefing and question-answer assistant
- Invoice detail extraction with Google Gemini

## Requirements

- Python 3.12 recommended
- Windows PowerShell commands are shown below; macOS and Linux users can use their platform's virtual-environment activation command.

## Setup

Run these commands from the project directory:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Gemini is optional for general dashboard use. To enable Gemini-powered invoice extraction and AI CFO responses, create a `.env` file in the project directory:

```text
GEMINI_API_KEY=your_gemini_api_key
```

Keep the key private. The `.gitignore` file excludes `.env`. Without a Gemini key, the assistant can still provide ledger-based answers, but Gemini invoice extraction is unavailable.

**Invoice privacy:** Uploaded invoice documents are sent to Google Gemini for extraction. Do not upload confidential documents unless you are authorized to share them with that service.

## Run the application

From the project directory, with the virtual environment activated:

```powershell
uvicorn main:app --reload
```

Open the dashboard at <http://127.0.0.1:8000/>. Interactive API documentation is available at <http://127.0.0.1:8000/docs>.

On startup, the app creates the local SQLite database (`digital_cfo.db`) and seeds demo records when needed. The database is local and is excluded from Git.

## Run tests

From the project directory:

```powershell
python -m unittest discover -s tests -v
```

## Demo limitations

The included figures are synthetic and for demonstration only. The app does not connect to live bank accounts, GST, identity, Account Aggregator, or payment systems, and its output is not a credit decision.
