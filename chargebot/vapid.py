"""Generate the key pair for push notifications:  python -m chargebot.vapid

Put VAPID_PRIVATE_KEY in Render's environment. Keep it secret and stable: changing it means every
device has to turn notifications on again. The public key is derived from it automatically.
"""

from .push import generate_keys

if __name__ == "__main__":
    keys = generate_keys()
    print(f"VAPID_PRIVATE_KEY={keys['VAPID_PRIVATE_KEY']}")
    print(f"# public key (derived automatically, shown for reference): {keys['VAPID_PUBLIC_KEY']}")
