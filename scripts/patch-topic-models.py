"""Keep Telegram model picker callbacks bound to their original session."""
from pathlib import Path


def patch(root):
    path = Path(root) / "gateway/platforms/telegram.py"
    text = path.read_text()
    old = """            # Store picker state keyed by chat_id
            self._model_picker_state[str(chat_id)] = {"""
    new = """            # Bind each picker message to its original topic/session.
            picker_key = (str(chat_id), msg.message_id)
            self._model_picker_state[picker_key] = {"""
    if new in text:
        return
    assert text.count(old) == 1, "Telegram picker storage changed"
    text = text.replace(old, new, 1)
    old = """        state = self._model_picker_state.get(chat_id)"""
    new = """        message = getattr(query, "message", None)
        picker_key = (str(chat_id), getattr(message, "message_id", None))
        state = self._model_picker_state.get(picker_key)"""
    assert text.count(old) == 1, "Telegram picker lookup changed"
    text = text.replace(old, new, 1)
    old = "self._model_picker_state.pop(chat_id, None)"
    assert text.count(old) == 2, "Telegram picker cleanup changed"
    text = text.replace(old, "self._model_picker_state.pop(picker_key, None)")
    compile(text, str(path), "exec")
    path.write_text(text)
