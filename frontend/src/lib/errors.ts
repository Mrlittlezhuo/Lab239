/**
 * 统一 API 错误。FastAPI 错误体是 {detail}；知识库等旧接口是
 * HTTP 200 + {code, msg} 信封，信封错误由 feature client 抛本类。
 */
export class ApiError extends Error {
  readonly status: number;
  readonly detail?: unknown;
  readonly code?: number;

  constructor(
    status: number,
    message: string,
    opts?: { detail?: unknown; code?: number },
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = opts?.detail;
    this.code = opts?.code;
  }

  static async fromResponse(res: Response): Promise<ApiError> {
    let detail: unknown;
    try {
      const body = await res.json();
      detail = (body as { detail?: unknown }).detail ?? body;
    } catch {
      try {
        detail = await res.text();
      } catch {
        detail = undefined;
      }
    }
    const message =
      typeof detail === "string" && detail
        ? detail
        : `请求失败（HTTP ${res.status}）`;
    return new ApiError(res.status, message, { detail });
  }

  /** 服务端“请先修改密码”信号（403）。 */
  isPasswordRequired(): boolean {
    return (
      this.status === 403 &&
      typeof this.detail === "string" &&
      this.detail.includes("请先修改密码")
    );
  }
}

/** 知识库等旧接口的 {code,msg,data} 信封（HTTP 200 也可能是错误）。 */
export interface Envelope<T = unknown> {
  code: number;
  msg?: string;
  data?: T;
}

export function unwrapEnvelope<T>(body: Envelope<T>): T {
  checkEnvelope(body);
  return body.data as T;
}

/** 校验信封并原样返回（写操作需要 msg 展示给用户时用）。 */
export function checkEnvelope<T>(body: Envelope<T>): Envelope<T> {
  if (body.code !== 200) {
    throw new ApiError(200, body.msg || `操作失败（code=${body.code}）`, {
      code: body.code,
    });
  }
  return body;
}
