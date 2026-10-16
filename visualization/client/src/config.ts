// Loopback only. Runtime port configuration must match the local server.
export const SERVER_URL = `http://127.0.0.1:${import.meta.env.VITE_SERVER_PORT || '4444'}`;
export const WS_URL = `${SERVER_URL.replace('http:', 'ws:')}/ws`;
