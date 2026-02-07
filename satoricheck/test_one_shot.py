#!/usr/bin/env python3
"""
Test script for One-Shot Thinking batch analysis.
"""
import requests
import json
import time

BASE_URL = "http://127.0.0.1:8000"

def test_one_shot_batch():
    """Test the new One-Shot batch analysis."""
    
    # Test claims - mix of easy and harder ones
    claims = [
        "The capital of France is Paris.",
        "Water boils at 100 degrees Celsius at sea level.",
        "The Great Wall of China is visible from the Moon with naked eye."
    ]
    
    context = "This is a short test context that won't trigger caching."
    
    payload = {
        "claims": claims,
        "context": context
    }
    
    print("Testing One-Shot Batch Analysis...")
    print(f"Claims: {len(claims)}")
    print("-" * 50)
    
    start = time.time()
    
    try:
        response = requests.post(
            f"{BASE_URL}/api/factcheck/analyze-batch",
            json=payload,
            timeout=120
        )
        
        elapsed = time.time() - start
        print(f"Response in {elapsed:.1f}s (Status: {response.status_code})")
        
        if response.status_code == 200:
            data = response.json()
            results = data.get('results', [])
            
            print(f"\n✅ Got {len(results)} results:")
            for i, r in enumerate(results):
                verdict = r.get('verdict', 'N/A')
                explanation = r.get('explanation', '')[:80]
                sources = r.get('sources', [])
                print(f"  {i+1}. {verdict}: {explanation}...")
                if sources:
                    print(f"     Sources: {sources[:2]}")
        else:
            print(f"❌ Error: {response.text[:500]}")
            
    except Exception as e:
        print(f"❌ Request failed: {e}")

if __name__ == "__main__":
    test_one_shot_batch()
