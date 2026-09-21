import { telegram } from './telegram';
import { User, Subscription, SummaryStats, ChannelPair, StorySettings, StoryQueueItem, AudioTrack, SystemTelemetry } from '../types';

const API_BASE = '/api';

interface RequestOptions extends RequestInit {
  timeout?: number;
}

async function request<T>(endpoint: string, options: RequestOptions = {}): Promise<T> {
  const userId = telegram.getUserId();
  const initData = telegram.getInitData();
  
  const headers = {
    'Content-Type': 'application/json',
    'X-User-Id': String(userId),
    'X-Telegram-Init-Data': initData || '',
    ...(options.headers || {})
  };

  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), options.timeout || 15000);

  try {
    const res = await fetch(`${API_BASE}${endpoint}`, {
      ...options,
      headers,
      signal: controller.signal
    });
    
    clearTimeout(id);
    
    if (!res.ok) {
      const errorData = await res.json().catch(() => ({}));
      throw new Error(errorData.error || `HTTP ${res.status}: ${res.statusText}`);
    }
    return await res.json();
  } catch (err) {
    clearTimeout(id);
    console.warn(`API call ${endpoint} failed:`, err);
    throw err;
  }
}

export const api = {
  async getMe(): Promise<{ user: User; subscription: Subscription; stats: SummaryStats }> {
    return request('/auth.php');
  },

  async getPairs(): Promise<{ pairs: ChannelPair[] }> {
    return request('/pairs.php');
  },

  async createPair(data: Partial<ChannelPair>): Promise<{ pair_id: number; message: string }> {
    return request('/pairs.php', {
      method: 'POST',
      body: JSON.stringify(data)
    });
  },

  async updatePair(id: number, data: Partial<ChannelPair>): Promise<{ message: string }> {
    return request(`/pairs.php?action=update&id=${id}`, {
      method: 'PUT',
      body: JSON.stringify(data)
    });
  },

  async togglePair(id: number): Promise<{ is_active: boolean }> {
    return request(`/pairs.php?action=toggle&id=${id}`, {
      method: 'POST'
    });
  },

  async deletePair(id: number): Promise<{ message: string }> {
    return request(`/pairs.php?action=delete&id=${id}`, {
      method: 'DELETE'
    });
  },

  async sendTestPost(id: number): Promise<{ message: string }> {
    return request(`/pairs.php?action=test&id=${id}`, {
      method: 'POST'
    });
  },

  async triggerBackfill(id: number, count: number): Promise<{ message: string; count: number }> {
    const res = await request<any>('/backfill.php', {
      method: 'POST',
      body: JSON.stringify({ pair_id: id, limit: count })
    });
    return { message: res.message, count: res.limit };
  },

  async getStorySettings(): Promise<{ settings: StorySettings }> {
    return request('/story.php?action=settings');
  },

  async saveStorySettings(data: Partial<StorySettings>): Promise<{ message: string }> {
    return request('/story.php?action=settings', {
      method: 'POST',
      body: JSON.stringify(data)
    });
  },

  async getStoryQueue(): Promise<{ queue: StoryQueueItem[]; posted: any[] }> {
    const res = await request<any>('/story.php?action=queue');
    return { queue: res.queue || [], posted: [] };
  },

  async getAudioTracks(): Promise<{ tracks: AudioTrack[] }> {
    return request('/audio.php');
  },

  async getSystemStatus(): Promise<{ telemetry: SystemTelemetry; logs: string[] }> {
    return request('/system.php');
  },
  async getFeed(): Promise<{ feed: any[] }> {
    return request('/feed.php');
  },
  
  async checkout(tier: string): Promise<{ invoice_link: string; stars: number; message: string }> {
    return request('/billing.php?action=checkout', {
      method: 'POST',
      body: JSON.stringify({ tier })
    });
  }
};
