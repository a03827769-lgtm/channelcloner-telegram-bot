import { telegram } from './telegram';
import {
  User, Subscription, SummaryStats, ChannelPair, NewPairRequest, StorySettings, StoryQueueItem,
  StoryPostedItem, AudioTrack, SystemTelemetry, StoreProduct, StoreOrder, StoreQuote, FeedItem,
  BillingCatalog
} from '../types';

const API_BASE = '/api';
const DEFAULT_TIMEOUT_MS = 15000;

interface RequestOptions extends RequestInit {
  timeout?: number;
}

export class ApiError extends Error {
  constructor(message: string, public status: number, public code?: string, public retryAfter?: number) {
    super(message);
    this.name = 'ApiError';
  }
}

interface ErrorBody {
  error?: string;
  code?: string;
  retry_after?: number;
}

/** fetch() rejects with an AbortError (a DOMException in browsers, a plain Error in some webviews). */
export function isAbortError(err: unknown): boolean {
  return typeof err === 'object' && err !== null && (err as { name?: unknown }).name === 'AbortError';
}

/**
 * Every request is authenticated with Telegram's signed WebApp initData.
 * The backend verifies the HMAC signature; no client-supplied user id is ever sent or trusted.
 */
async function request<T>(endpoint: string, options: RequestOptions = {}): Promise<T> {
  const { timeout, headers: extraHeaders, ...init } = options;
  const headers: Record<string, string> = {
    'X-Telegram-Init-Data': telegram.getInitData(),
    ...((extraHeaders as Record<string, string>) || {})
  };
  if (init.body !== undefined) {
    headers['Content-Type'] = 'application/json';
  }

  const controller = new AbortController();
  const outerSignal = init.signal;
  const onOuterAbort = () => controller.abort();
  if (outerSignal) {
    if (outerSignal.aborted) controller.abort();
    else outerSignal.addEventListener('abort', onOuterAbort);
  }
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeout || DEFAULT_TIMEOUT_MS);

  try {
    let res: Response;
    try {
      res = await fetch(`${API_BASE}${endpoint}`, { ...init, headers, signal: controller.signal });
    } catch (err) {
      if (isAbortError(err)) {
        if (!timedOut && outerSignal?.aborted) throw err; // cancelled by the caller
        throw new ApiError("Server javob bermadi. Internet aloqasini tekshirib, qayta urinib ko'ring.", 0, 'TIMEOUT');
      }
      throw new ApiError("Tarmoq xatosi: serverga ulanib bo'lmadi. Internet aloqasini tekshiring.", 0, 'NETWORK_ERROR');
    }
    const data = (await res.json().catch(() => ({}))) as unknown;
    if (!res.ok) {
      const body = (data && typeof data === 'object' ? data : {}) as ErrorBody;
      const retryHeader = Number(res.headers.get('Retry-After'));
      const retryAfter = body.retry_after ?? (Number.isFinite(retryHeader) && retryHeader > 0 ? retryHeader : undefined);
      throw new ApiError(body.error || `Server xatosi (HTTP ${res.status})`, res.status, body.code, retryAfter);
    }
    return data as T;
  } finally {
    clearTimeout(timer);
    outerSignal?.removeEventListener('abort', onOuterAbort);
  }
}

const json = (method: string, body?: unknown): RequestOptions => ({
  method,
  body: body === undefined ? undefined : JSON.stringify(body)
});

/** Uzbek guidance shown under the server's message for errors the user can fix. */
const ERROR_HINTS: Record<string, string> = {
  USER_NOT_ADMIN: "Faqat o'zingiz egasi yoki post joylash huquqiga ega administratori bo'lgan kanalni ulash mumkin.",
  BOT_NOT_ADMIN: "Kanal sozlamalari → Administratorlar bo'limida botni qo'shing va unga «Xabar joylash» huquqini bering, so'ng qayta urinib ko'ring.",
  TARGET_NOT_FOUND: "Botni kanalingizga administrator qilib qo'shing va @username yoki kanal ID sini to'g'ri kiriting.",
  TARGET_INVALID: "Maqsad sifatida faqat kanal yoki superguruhning @username yoki ID sini kiriting.",
  PLAN_LIMIT: "Tarifingizdagi kanal limiti to'lgan. Boshqa kanalni to'xtating yoki «Tariflar» bo'limidan tarifni oshiring.",
  SUBSCRIPTION_INACTIVE: "Obuna yoki sinov muddati tugagan. Davom etish uchun «Tariflar» bo'limidan tarifni yangilang.",
  PRO_REQUIRED: "Bu imkoniyat PRO va VIP tariflarida mavjud. «Tariflar» bo'limidan tarifni oshiring.",
  VIP_REQUIRED: "Bu imkoniyat faqat VIP tarifida mavjud. «Tariflar» bo'limidan VIP ga o'ting.",
  RATE_LIMITED: "Juda ko'p so'rov yuborildi. Bir oz kutib, qayta urinib ko'ring.",
  PAIR_CYCLE: "A → B va B → A kabi halqa postlarni cheksiz qayta ko'chiradi, shuning uchun bunday ulanishga ruxsat berilmaydi.",
  SELF_LOOP: "Manba va maqsad sifatida ikki xil kanalni tanlang.",
  DUPLICATE: "Bu juftlik allaqachon ro'yxatingizda bor.",
  SOURCE_NOT_FOUND: "Manba kanal ommaviy ekanini yoki havola to'g'riligini tekshiring.",
  TIMEOUT: "Internet aloqasini tekshirib, qayta urinib ko'ring.",
  NETWORK_ERROR: "Internet aloqasini tekshirib, qayta urinib ko'ring.",
};

export interface ErrorExplanation {
  message: string;
  hint?: string;
  code?: string;
}

/** Turns any thrown value into a user-facing Uzbek message plus an optional hint. */
export function explainError(err: unknown, fallback: string): ErrorExplanation {
  if (err instanceof ApiError) {
    let message = err.message || fallback;
    if (err.code === 'RATE_LIMITED' && err.retryAfter && !message.includes(String(err.retryAfter))) {
      message = `${message} (${err.retryAfter} soniya)`;
    }
    const hint = err.code ? ERROR_HINTS[err.code] : undefined;
    return { message, hint: hint && hint !== message ? hint : undefined, code: err.code };
  }
  if (err instanceof Error && err.message) {
    return { message: err.message };
  }
  return { message: fallback };
}

/** Single-line version for toasts. */
export function errorText(err: unknown, fallback: string): string {
  const { message, hint } = explainError(err, fallback);
  return hint ? `${message} ${hint}` : message;
}

export const api = {
  getMe(): Promise<{ user: User; subscription: Subscription; stats: SummaryStats; billing?: BillingCatalog }> {
    return request('/me');
  },

  getFeed(): Promise<{ feed: FeedItem[] }> {
    return request('/feed');
  },

  getPairs(): Promise<{ pairs: ChannelPair[] }> {
    return request('/pairs');
  },

  createPair(data: NewPairRequest): Promise<{ pair_id: number; pair: ChannelPair | null; message: string }> {
    return request('/pairs', json('POST', data));
  },

  /** Sends only the changed fields; the server applies plan gates to what actually changes. */
  updatePair(id: number, changes: Partial<ChannelPair>): Promise<{ message: string; pair: ChannelPair }> {
    return request(`/pairs/${id}`, json('PUT', changes));
  },

  togglePair(id: number): Promise<{ is_active: boolean }> {
    return request(`/pairs/${id}/toggle`, json('POST'));
  },

  deletePair(id: number): Promise<{ message: string }> {
    return request(`/pairs/${id}`, json('DELETE'));
  },

  sendTestPost(id: number): Promise<{ message: string }> {
    return request(`/pairs/${id}/test-post`, json('POST'));
  },

  triggerBackfill(id: number, count: number): Promise<{ message: string; count: number }> {
    return request(`/pairs/${id}/backfill`, json('POST', { count }));
  },

  getStorySettings(): Promise<{ settings: StorySettings }> {
    return request('/story/settings');
  },

  saveStorySettings(changes: Partial<StorySettings>): Promise<{ message: string; settings: StorySettings }> {
    return request('/story/settings', json('POST', changes));
  },

  getStoryQueue(): Promise<{ queue: StoryQueueItem[]; posted: StoryPostedItem[] }> {
    return request('/story/queue');
  },

  getAudioTracks(): Promise<{ tracks: AudioTrack[] }> {
    return request('/audio-tracks');
  },

  getSystemStatus(): Promise<{ telemetry: SystemTelemetry; logs: string[]; is_admin?: boolean }> {
    return request('/system');
  },

  checkout(tier: 'pro' | 'vip'): Promise<{ invoice_link: string; stars: number; message: string }> {
    return request('/billing/checkout', json('POST', { tier }));
  },

  getStoreProducts(category?: string, onlyAvailable: boolean = true): Promise<{ products: StoreProduct[] }> {
    const params = new URLSearchParams();
    if (category) params.append('category', category);
    if (onlyAvailable) params.append('only_available', 'true');
    const query = params.toString() ? `?${params.toString()}` : '';
    return request(`/store/products${query}`);
  },

  getStoreCategories(): Promise<{ categories: string[] }> {
    return request('/store/categories');
  },

  /** Authoritative price of an order (the same computation the invoice uses). */
  getStoreQuote(productId: number, quantity: number, signal?: AbortSignal): Promise<StoreQuote> {
    const params = new URLSearchParams({ product_id: String(productId), quantity: String(quantity) });
    return request(`/store/quote?${params.toString()}`, { signal });
  },

  buyStoreProduct(productId: number, quantity: number, targetLink: string): Promise<{
    success: boolean;
    status: 'awaiting_payment';
    order_id: number;
    price_stars: number;
    invoice_link: string;
    message: string;
  }> {
    return request('/store/buy', json('POST', { product_id: productId, quantity, target_link: targetLink }));
  },

  getStoreOrders(): Promise<{ orders: StoreOrder[] }> {
    return request('/store/orders');
  }
};
