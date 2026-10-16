import { expect, test } from 'bun:test';
import { allowLocalRequest } from './local-access';

test('only loopback hosts and the configured local frontend origin are accepted', () => {
  const allow = (host: string, origin?: string) => allowLocalRequest(new Request(`http://${host}/api/agents`, {
    headers: origin ? { origin } : {},
  }), 4444, 4445);
  expect(allow('127.0.0.1:4444')).toBe(true);
  expect(allow('localhost:4444', 'http://127.0.0.1:4445')).toBe(true);
  expect(allow('127.0.0.1:4444', 'https://foreign.example')).toBe(false);
  expect(allow('rebinding.example:4444')).toBe(false);
  expect(allow('192.168.0.1:4444')).toBe(false);
  expect(allow('localhost:4444', 'null')).toBe(false);
});
