#!/usr/bin/env python3
"""RAT entry point. Run with: python run.py"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("rat.app:app", host="127.0.0.1", port=8000, log_level="info")
