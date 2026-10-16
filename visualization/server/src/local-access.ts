/** Applied to HTTP and upgrades before Hono; CORS alone does not guard WebSockets. */
export function allowLocalRequest(request: Request, port: number, clientPort: number): boolean {
  const url = new URL(request.url);
  if (!['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) return false;
  if (url.port !== String(port)) return false;
  const origin = request.headers.get('origin');
  if (origin === null) return true; // Local CLI health probes have no browser origin.
  return ['127.0.0.1', 'localhost', '[::1]'].some((host) => origin === `http://${host}:${clientPort}`);
}
