import secrets

API_KEYS = {
    "subsync-demo-key-2026": "Demo key",
}

def generate_key():
    return "subsync-" + secrets.token_hex(16)

def is_valid_key(key):
    return key in API_KEYS

if __name__ == "__main__":
    print("Yeni API key:")
    print(generate_key())
