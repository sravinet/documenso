import { describe, expect, it } from 'vitest';

import { encodeBase64 } from '../src/encoding';

describe('encodeBase64', () => {
  it('encodes known bytes', () => {
    expect(encodeBase64(new Uint8Array([0x4d, 0x61, 0x6e]))).toBe('TWFu');
    expect(encodeBase64(new Uint8Array([]))).toBe('');
  });

  it('round-trips binary data larger than one chunk', () => {
    const bytes = new Uint8Array(100_000).map((_, index) => (index * 31) % 256);

    const decoded = Uint8Array.from(atob(encodeBase64(bytes)), (char) => char.charCodeAt(0));

    expect(decoded).toStrictEqual(bytes);
  });
});
