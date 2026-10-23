export type User = { id: string; username: string };

export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message);
  }
}

async function responseFor(path: string, body?: object, method?: 'POST' | 'PUT' | 'DELETE'): Promise<Response> {
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

export async function request<T>(path: string, body?: object, method?: 'POST' | 'PUT' | 'DELETE'): Promise<T> {
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
