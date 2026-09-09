# Antigravity AI Proxy

High-performance lightweight OpenAI-compatible API Proxy built with FastAPI.
Translates OpenAI chat completion requests directly to Google Gemini or other upstreams.

## Features
- **OpenAI Compatible Endpoint**: Exposes `/v1/chat/completions` and `/v1/models`
- **Native Gemini Schema Translation**: Converts OpenAI `json_schema` response_format directly to Gemini `responseSchema`
- **Fast & Lightweight**: Built on FastAPI and Uvicorn
- **Zero Configuration Fallback**: Works with Gemini API Key or OAuth token sessions

## Quick Start
1. Copy `.env.example` to `.env` and put your `GEMINI_API_KEY`
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Run the proxy server:
   ```bash
   uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```
   Or double-click `start_proxy.bat`
