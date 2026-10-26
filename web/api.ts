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

export type RequestOptions = { signal?: AbortSignal; timeoutMs?: number };
export const SESSION_EXPIRED_EVENT = 'lisa:session-expired';
let sessionExpired = false;
export function resumeSession() { sessionExpired = false; }
const expiredMessage = '登录已失效。当前未保存内容仍保留，请重新登录原账号后再操作。';

async function withResponse<T>(path: string, body: object | undefined, method: 'POST' | 'PUT' | 'DELETE' | undefined, options: RequestOptions, consume: (response: Response) => Promise<T>): Promise<T> {
  // Keep the mounted editor intact and stop polling/writes until the original account is checked.
  if (sessionExpired && !path.startsWith('/auth/')) throw new ApiError(expiredMessage, 401);
  const writing = body !== undefined || method !== undefined;
  const controller = new AbortController();
  const cancel = () => controller.abort(options.signal?.reason);
  if (options.signal?.aborted) cancel();
  else options.signal?.addEventListener('abort', cancel, { once: true });
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, options.timeoutMs ?? (writing ? 120_000 : 30_000));
  try {
    const response = await fetch(`/api${path}`, {
      method: method ?? (body === undefined ? 'GET' : 'POST'),
      credentials: 'same-origin',
      headers: body === undefined && method === undefined ? undefined : {
        ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
        'X-LISA-Request': '1',
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
    if (!response.ok) {
      if (response.status === 401 && !path.startsWith('/auth/')) {
        sessionExpired = true;
        window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
        throw new ApiError(expiredMessage, 401);
      }
      const error = await response.json().catch(() => ({}));
      throw new ApiError(
        typeof error.detail === 'string' ? error.detail
          : response.status === 422 ? '填写的信息不符合要求，请检查后重试。'
            : '暂时无法完成操作，请稍后重试。', response.status,
      );
    }
    return await consume(response);
  } catch (cause) {
    if (options.signal?.aborted) throw options.signal.reason ?? new DOMException('请求已取消', 'AbortError');
    if (timedOut) throw new Error(writing
      ? '等待响应超时，操作结果尚未确认。输入仍保留，请先核对是否已保存，再决定是否重试。'
      : '读取超时，请检查网络后重试。');
    if (cause instanceof ApiError) throw cause;
    throw new Error(writing
      ? '连接中断，操作结果尚未确认。输入仍保留，请先核对是否已保存，再决定是否重试。'
      : '无法读取书房，请检查网络后重试。');
  } finally {
    clearTimeout(timer);
    options.signal?.removeEventListener('abort', cancel);
  }
}

export async function request<T>(path: string, body?: object, method?: 'POST' | 'PUT' | 'DELETE', options: RequestOptions = {}): Promise<T> {
  return withResponse(path, body, method, options, response => response.status === 204 ? Promise.resolve(undefined as T) : response.json());
}

export async function downloadBody(projectId: string) {
  const { name, blob } = await withResponse(`/projects/${projectId}/export`, undefined, undefined, {}, async response => ({
    name: response.headers.get('Content-Disposition')?.split("filename*=UTF-8''")[1], blob: await response.blob(),
  }));
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url; link.download = name ? decodeURIComponent(name) : '作品正文.txt';
  document.body.appendChild(link); link.click(); link.remove();
  // Let the browser consume the download before releasing its object URL.
  setTimeout(() => URL.revokeObjectURL(url), 1_000);
}
