export type User = { id: string; username: string };

export type StyleKind = 'personality' | 'writing';
export type StyleReport = Record<string, unknown>;
export type StyleVersion = { id: string; version: number; prompt: string; summary: string; report: StyleReport; created_at: string; source_task_id?: string | null };
export type StyleProfile = { id: string; kind: StyleKind; name: string; latest_version: number; versions: StyleVersion[] };
export type StyleApplications = { personality: string | null; writing: string | null };
export type StyleLibrary = { profiles: StyleProfile[]; applications: StyleApplications };
export type WritingStyle = { override_version_id: string | null; effective_version_id: string | null; effective_profile_name: string | null; prompt: string; source: 'override' | 'default' | 'none' };

// Encode bytes rather than decoding in the browser so the parser can identify UTF-8/GB18030.
export async function styleFile(file: File): Promise<{ name: string; content_base64: string }> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = '';
  for (let index = 0; index < bytes.length; index += 8192) binary += String.fromCharCode(...bytes.subarray(index, index + 8192));
  return { name: file.name, content_base64: btoa(binary) };
}

export function abandonStyleCorpusOnClose(id: string) {
  // Best effort on tab close; server expiry also covers disconnected clients.
  void fetch(`/api/style/corpora/${id}`, { method: 'DELETE', credentials: 'same-origin', headers: { 'X-LISA-Request': '1' }, keepalive: true }).catch(() => {});
}

export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message);
  }
}

async function responseFor(path: string, body?: object, method?: 'POST' | 'PUT' | 'DELETE' | 'PATCH'): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      method: method ?? (body === undefined ? 'GET' : 'POST'),
      credentials: 'same-origin',
      headers: body === undefined && method === undefined ? undefined : {
        ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
        'X-LISA-Request': '1',
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new Error('无法连接书房，请检查网络后重试。');
  }

  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new ApiError(
      typeof error.detail === 'string'
        ? error.detail
        : response.status === 422
          ? '填写的信息不符合要求，请检查后重试。'
          : '暂时无法完成操作，请稍后重试。',
      response.status,
    );
  }

  return response;
}

export async function request<T>(path: string, body?: object, method?: 'POST' | 'PUT' | 'DELETE' | 'PATCH'): Promise<T> {
  const response = await responseFor(path, body, method);
  return response.status === 204 ? undefined as T : response.json();
}

export async function downloadBody(projectId: string) {
  const response = await responseFor(`/projects/${projectId}/export`);
  const name = response.headers.get('Content-Disposition')!.split("filename*=UTF-8''")[1];
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement('a');
  link.href = url; link.download = decodeURIComponent(name);
  document.body.appendChild(link); link.click(); link.remove();
  // Let the browser consume the download before releasing its object URL.
  setTimeout(() => URL.revokeObjectURL(url), 1_000);
}
