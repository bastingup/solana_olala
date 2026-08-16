// Formatting helpers shared across panels and the galaxy.

export function fmtSol(value, digits = 3) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${Number(value).toFixed(digits)} ◎`;
}

export function fmtUsd(value) {
  if (!value) return "";
  return value.toLocaleString("en-US", {
    style: "currency", currency: "USD", maximumFractionDigits: 0,
  });
}

export function fmtPct(value, digits = 0) {
  if (value === null || value === undefined) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

export function fmtSigned(value, digits = 3) {
  if (value === null || value === undefined) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${Number(value).toFixed(digits)} ◎`;
}

export function shortAddr(address, keep = 4) {
  if (!address) return "?";
  if (address.length <= keep * 2 + 1) return address;
  return `${address.slice(0, keep)}…${address.slice(-keep)}`;
}

export function timeAgo(ts) {
  if (!ts) return "";
  const seconds = Math.max(0, Date.now() / 1000 - ts);
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

export function clockTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString("en-GB", {
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

const HTML_ESCAPES = {
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
};

// Escapes text for BOTH element content and attribute values. Chain-sourced
// strings (token symbols, addresses, error text) are attacker-chosen; every
// interpolation into markup must pass through here.
export function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => HTML_ESCAPES[c]);
}
