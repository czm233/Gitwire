export class UnauthorizedError extends Error {
  constructor() {
    super("unauthorized");
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { "content-type": "application/json" },
    ...init,
  });
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) {
    const body = await res.json().catch(() => ({} as { detail?: string }));
    throw new Error(body.detail || `${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

export const post = <T>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body ? JSON.stringify(body) : undefined });

export const patch = <T>(path: string, body?: unknown) =>
  api<T>(path, { method: "PATCH", body: body ? JSON.stringify(body) : undefined });

export const del = <T>(path: string) => api<T>(path, { method: "DELETE" });
