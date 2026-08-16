// WebSocket client with exponential-backoff reconnect. The stream is the
// only source of state; on reconnect the server replays a full snapshot.

const BACKOFF_START_MS = 500;
const BACKOFF_MAX_MS = 15000;

export class StreamClient {
  constructor(store) {
    this._store = store;
    this._backoff = BACKOFF_START_MS;
    this._socket = null;
  }

  connect() {
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    this._store.setConnection("connecting");
    this._socket = new WebSocket(`${protocol}://${location.host}/ws`);

    this._socket.onopen = () => {
      this._backoff = BACKOFF_START_MS;
      this._store.setConnection("open");
    };

    this._socket.onmessage = (message) => {
      let event;
      try {
        event = JSON.parse(message.data);
      } catch (error) {
        // A frame the backend produced but we cannot parse is a protocol
        // bug, not noise — swallowing it silently once hid a dead
        // snapshot behind a healthy-looking "connected" state.
        console.error("stream: dropping unparseable frame", error, message.data.slice(0, 200));
        return;
      }
      this._store.apply(event);
    };

    this._socket.onclose = () => {
      this._store.setConnection("closed");
      setTimeout(() => this.connect(), this._backoff);
      this._backoff = Math.min(this._backoff * 2, BACKOFF_MAX_MS);
    };

    this._socket.onerror = () => this._socket.close();
  }
}
