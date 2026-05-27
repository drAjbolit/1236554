AI Chatter Native Host 4.0.1
Branch 4 Local Bus + Native Messaging MVP.

Archive contains category folder: ai_chatter_native_host\

Install:
1. Place this folder as <AI_CHATTER_HOME>\ai_chatter_native_host.
2. Run install_native_host.bat.
3. Reload Chrome extension 4.0.1.
4. Extension popup should show nativeConnected=true.
5. Test:
   C:\Python314\python.exe <AI_CHATTER_HOME>\ai_chatter_executor\ai_chatter_bus_ping.py --home <AI_CHATTER_HOME>

If popup says native messaging host not found:
- Run check_native_host.bat
- Confirm registry points to ai_chatter.native_host.json
- Confirm manifest allowed_origins contains chrome-extension://kgdgnnlkagfboekpanbjbjiijonhbmhj/
