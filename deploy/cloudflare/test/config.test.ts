import { describe, expect, it } from 'vitest';

import {
  buildContainerEnvVars,
  instanceName,
  parseInstanceCount,
  pickInstanceIndex,
  withForwardedHeaders,
} from '../src/config';

describe('buildContainerEnvVars', () => {
  it('forwards string vars, drops bindings and worker-only vars, and pins PORT', () => {
    const binding = { get: () => null };

    expect(
      buildContainerEnvVars({
        NEXT_PUBLIC_WEBAPP_URL: 'https://sign.example.com',
        NEXT_PRIVATE_DATABASE_URL: 'postgres://db',
        CONTAINER_INSTANCE_COUNT: '3',
        SIGNING_CERT_R2_KEY: 'cert.p12',
        SIGNING_CERT_BUCKET: binding,
        PORT: '8080',
      }),
    ).toStrictEqual({
      NEXT_PUBLIC_WEBAPP_URL: 'https://sign.example.com',
      NEXT_PRIVATE_DATABASE_URL: 'postgres://db',
      PORT: '3000',
    });
  });

  it('keeps empty strings so explicit blanks reach the app', () => {
    expect(buildContainerEnvVars({ NEXT_PRIVATE_SMTP_HOST: '' })).toStrictEqual({
      NEXT_PRIVATE_SMTP_HOST: '',
      PORT: '3000',
    });
  });
});

describe('parseInstanceCount', () => {
  it('defaults to one instance when unset or blank', () => {
    expect(parseInstanceCount(undefined)).toBe(1);
    expect(parseInstanceCount('  ')).toBe(1);
  });

  it('parses a valid count', () => {
    expect(parseInstanceCount('4')).toBe(4);
  });

  it.each(['0', '-1', '1.5', 'two', '51'])('rejects %s', (raw) => {
    expect(() => parseInstanceCount(raw)).toThrow(RangeError);
  });
});

describe('pickInstanceIndex', () => {
  it('maps [0, 1) evenly onto instance indexes', () => {
    expect(pickInstanceIndex(3, 0)).toBe(0);
    expect(pickInstanceIndex(3, 0.34)).toBe(1);
    expect(pickInstanceIndex(3, 0.999)).toBe(2);
  });

  it('rejects out-of-range random values', () => {
    expect(() => pickInstanceIndex(3, 1)).toThrow(RangeError);
    expect(() => pickInstanceIndex(3, -0.1)).toThrow(RangeError);
  });
});

describe('instanceName', () => {
  it('names instances deterministically', () => {
    expect(instanceName(0)).toBe('instance-0');
    expect(instanceName(7)).toBe('instance-7');
  });
});

describe('withForwardedHeaders', () => {
  it('sets forwarded headers from the Cloudflare client IP, overriding spoofed values', () => {
    const request = new Request('https://sign.example.com/api/health', {
      headers: {
        'cf-connecting-ip': '203.0.113.9',
        'x-forwarded-for': '10.0.0.1',
        cookie: 'session=abc',
      },
    });

    const forwarded = withForwardedHeaders(request);

    expect(forwarded.headers.get('x-forwarded-for')).toBe('203.0.113.9');
    expect(forwarded.headers.get('x-real-ip')).toBe('203.0.113.9');
    expect(forwarded.headers.get('x-forwarded-proto')).toBe('https');
    expect(forwarded.headers.get('x-forwarded-host')).toBe('sign.example.com');
    expect(forwarded.headers.get('cookie')).toBe('session=abc');
    expect(forwarded.url).toBe('https://sign.example.com/api/health');
  });

  it('strips client-supplied IP headers when no Cloudflare client IP is present', () => {
    const request = new Request('http://localhost:8787/', {
      headers: { 'x-forwarded-for': '10.0.0.1', 'x-real-ip': '10.0.0.1' },
    });

    const forwarded = withForwardedHeaders(request);

    expect(forwarded.headers.get('x-forwarded-for')).toBeNull();
    expect(forwarded.headers.get('x-real-ip')).toBeNull();
    expect(forwarded.headers.get('x-forwarded-proto')).toBe('http');
  });

  it('preserves method and body for uploads', async () => {
    const request = new Request('https://sign.example.com/api/files', {
      method: 'POST',
      body: 'pdf-bytes',
    });

    const forwarded = withForwardedHeaders(request);

    expect(forwarded.method).toBe('POST');
    expect(await forwarded.text()).toBe('pdf-bytes');
  });
});
