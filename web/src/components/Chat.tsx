import { useRef, useState } from "react";
import { chat, type ChatMsg } from "../api";

export default function Chat() {
  const [msgs, setMsgs] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const listRef = useRef<HTMLDivElement | null>(null);

  const send = async () => {
    const text = input.trim();
    if (!text || sending) return;
    const next: ChatMsg[] = [...msgs, { role: "user", content: text }];
    setMsgs(next);
    setInput("");
    setSending(true);
    try {
      const reply = await chat(text, next);
      setMsgs((m) => [...m, { role: "assistant", content: reply }]);
    } catch {
      setMsgs((m) => [...m, { role: "assistant", content: "(I couldn't reach you just now.)" }]);
    } finally {
      setSending(false);
      requestAnimationFrame(() => listRef.current?.scrollTo(0, listRef.current.scrollHeight));
    }
  };

  return (
    <div className="chat">
      <div className="section-label">Chat with the opponent</div>
      <div className="chat-list" ref={listRef}>
        {msgs.length === 0 && <div className="empty">Say hi, ask a rules question, or talk trash.</div>}
        {msgs.map((m, i) => (
          <div className={`chat-msg ${m.role}`} key={i}>
            {m.content}
          </div>
        ))}
        {sending && <div className="chat-msg assistant chat-typing">…</div>}
      </div>
      <div className="chat-input">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
          placeholder="Message the opponent…"
          disabled={sending}
        />
        <button className="btn" onClick={send} disabled={sending || !input.trim()} type="button">
          Send
        </button>
      </div>
    </div>
  );
}
