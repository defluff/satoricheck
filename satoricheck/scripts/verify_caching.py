
"""
Verification Script for Gemini Context Caching.
"""
import sys
import os
import logging
import time

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.services.gemini_service import GeminiService
from backend.config import Config

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("CacheVerify")

def test_caching():
    print("-" * 50)
    print("TESTING CONTEXT CACHING")
    print("-" * 50)

    if not Config.GEMINI_API_KEY:
        print("❌ Error: GEMINI_API_KEY not set.")
        return

    service = GeminiService()
    
    # 1. Define Content to Cache
    # A large-ish context with a specific "secret" fact
    secret_fact = "The SatoriCheck mascot is a cybernetic sloth named Speedy."
    content_to_cache = f"""
    SYSTEM CONTEXT:
    You are the SatoriCheck assistant. 
    Here is some confidential internal knowledge:
    1. SatoriCheck was founded in 2024.
    2. {secret_fact}
    3. The primary mission is to fight misinformation.
    """ * 100 # Repeat to make it "worth" caching (though API min is usually low for test)
    
    # 2. Create Cache
    print("\n📦 Creating Cache...")
    try:
        # This method doesn't exist yet - expected to fail or need implementation
        cache_name = service.create_cache(
            content=content_to_cache,
            ttl_minutes=5
        )
        print(f"✅ Cache Created! Name: {cache_name}")
    except AttributeError:
        print("❌ Error: 'create_cache' method not found in GeminiService.")
        return
    except Exception as e:
        print(f"❌ Cache Creation Failed: {e}")
        return

    # 3. Use Cache
    print("\n🔍 Querying Model (using Cache)...")
    try:
        # We ask about the secret fact WITHOUT providing it in the prompt
        # The model must read it from the cache
        prompt = "Who is the SatoriCheck mascot?"
        
        # This method doesn't exist yet
        result = service.generate_with_cache(
            cache_name=cache_name,
            prompt=prompt
        )
        
        print(f"\n🤖 Answer: {result.get('text', 'No text returned')}")
        
        if "Speedy" in result.get('text', ''):
            print("✅ SUCCESS: Model verified secret from cache!")
        else:
            print("❌ FAILURE: Model did not find the secret.")
            
    except Exception as e:
        print(f"❌ Cached Generation Failed: {e}")

if __name__ == "__main__":
    test_caching()
