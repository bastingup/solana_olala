// REST client: commands only. Reading state is the WebSocket's job.

async function request(method, path, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `${method} ${path} failed (${response.status})`);
  }
  return payload;
}

export const api = {
  unlockKeystore: (passphrase) =>
    request("POST", "/api/keystore/unlock", { passphrase }),
  addPaperWallet: (label, startingSol) =>
    request("POST", "/api/wallets", { paper: true, label, starting_sol: startingSol }),
  addLiveWallet: (label, secret) =>
    request("POST", "/api/wallets", { label, secret }),
  armWallet: (walletId, armed) =>
    request("POST", `/api/wallets/${walletId}/arm`, { armed }),
  assignTrader: (address, walletId) =>
    request("POST", `/api/traders/${address}/assign`,
            { wallet_id: walletId }),
  unfollowTrader: (address) =>
    request("POST", `/api/traders/${address}/unfollow`),
  closePosition: (positionId) =>
    request("POST", `/api/positions/${positionId}/close`),
  setMode: (mode) => request("POST", "/api/mode", { mode }),
  updateConfig: (patch) => request("PUT", "/api/config", patch),
};
