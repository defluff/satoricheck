
import os
import requests
import json
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv('GEMINI_API_KEY')
if not API_KEY:
    print("Error: GEMINI_API_KEY not found in .env")
    exit(1)

def create_cache(model_name, content, ttl="300s"):
    url = f"https://generativelanguage.googleapis.com/v1beta/cachedContents?key={API_KEY}"
    
    payload = {
        "model": f"models/{model_name}",
        "contents": [{
            "parts": [{"text": content}],
            "role": "user"
        }],
        "ttl": ttl
    }
    
    print(f"Testing Model: {model_name}")
    try:
        response = requests.post(url, json=payload, headers={'Content-Type': 'application/json'})
        if response.status_code == 200:
            print("  ✅ Success!")
            print(f"  Name: {response.json().get('name')}")
            # Delete it to be clean
            cache_name = response.json().get('name')
            requests.delete(f"https://generativelanguage.googleapis.com/v1beta/{cache_name}?key={API_KEY}")
        else:
            print(f"  ❌ Failed: {response.status_code}")
            print(f"  Response: {response.text}")
    except Exception as e:
        print(f"  ❌ Error: {e}")
    print("-" * 40)

def main():
    content = "This is a test context for caching. " * 50
    
    # 1. Gemini 1.5 Flash (Known to work)
    create_cache("gemini-1.5-flash-001", content)
    
    # 2. Gemini 3 Flash Preview (Current config)
    create_cache("gemini-3-flash-preview", content)
    
    # 3. Gemini 2.0 Flash Exp (Alternative)
    create_cache("gemini-2.0-flash-exp", content)

    # 4. Gemini 2.0 Flash Thinking Exp (Alternative)
    create_cache("gemini-2.0-flash-thinking-exp-1219", content)

if __name__ == "__main__":
    main()
