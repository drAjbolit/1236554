AI Chatter Browser Extension 4.0.0
Branch 4 Local Bus + Native Messaging MVP.

Purpose:
- Connect extension service worker to ai_chatter.native_host.
- Support bus.ping -> bus.pong through native messaging.
- Do not use legacy CDP/DOM guessing as primary transport.

Stable extension id is fixed by manifest key:
kgdgnnlkagfboekpanbjbjiijonhbmhj

Install:
1. Load unpacked extension directory in chrome://extensions.
2. Install native host using ai_chatter_native_host/install_native_host.bat.
3. Open extension popup and check nativeConnected.
