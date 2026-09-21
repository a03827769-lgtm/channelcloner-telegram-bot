declare global {
  interface Window {
    Telegram?: {
      WebApp: any;
    };
  }
}

class TelegramService {
  private tg = typeof window !== 'undefined' ? window.Telegram?.WebApp : null;

  init() {
    if (this.tg) {
      try {
        this.tg.ready();
        this.tg.expand();
        if (this.tg.enableClosingConfirmation) {
          this.tg.enableClosingConfirmation();
        }
        if (this.tg.setHeaderColor) {
          this.tg.setHeaderColor('#0E1621');
        }
        if (this.tg.setBackgroundColor) {
          this.tg.setBackgroundColor('#08090A');
        }
      } catch (e) {
        console.warn('Telegram WebApp init warning:', e);
      }
    }
  }

  isAvailable(): boolean {
    if (typeof window !== 'undefined' && window.Telegram?.WebApp) {
      this.tg = window.Telegram.WebApp;
    }
    return Boolean(this.tg && this.tg.initData && this.tg.initData.length > 0);
  }

  getInitData(): string {
    if (typeof window !== 'undefined' && window.Telegram?.WebApp) {
      this.tg = window.Telegram.WebApp;
    }
    return this.tg?.initData || '';
  }

  getUser() {
    if (this.tg?.initDataUnsafe?.user) {
      return this.tg.initDataUnsafe.user;
    }
    // Development fallback
    return {
      id: 99999999,
      first_name: 'VIP Developer',
      last_name: '',
      username: 'channelcloner_vip',
      is_premium: true
    };
  }

  getUserId(): number {
    return this.getUser().id;
  }

  // Haptic feedback
  impact(style: 'light' | 'medium' | 'heavy' | 'rigid' | 'soft' = 'light') {
    try {
      if (this.tg?.HapticFeedback?.impactOccurred) {
        this.tg.HapticFeedback.impactOccurred(style);
      } else if (navigator.vibrate) {
        navigator.vibrate(15);
      }
    } catch {
      // ignore
    }
  }

  notification(type: 'error' | 'success' | 'warning' = 'success') {
    try {
      if (this.tg?.HapticFeedback?.notificationOccurred) {
        this.tg.HapticFeedback.notificationOccurred(type);
      } else if (navigator.vibrate) {
        navigator.vibrate([20, 50, 20]);
      }
    } catch {
      // ignore
    }
  }

  selection() {
    try {
      if (this.tg?.HapticFeedback?.selectionChanged) {
        this.tg.HapticFeedback.selectionChanged();
      }
    } catch {
      // ignore
    }
  }

  openInvoice(slug: string, callback?: (status: string) => void) {
    if (this.tg?.openInvoice) {
      this.tg.openInvoice(slug, callback);
    } else {
      alert(`Telegram Stars invoice: ${slug}`);
    }
  }

  openLink(url: string) {
    if (this.tg?.openTelegramLink && url.includes('t.me/')) {
      this.tg.openTelegramLink(url);
    } else if (this.tg?.openLink) {
      this.tg.openLink(url);
    } else {
      window.open(url, '_blank');
    }
  }

  close() {
    if (this.tg?.close) {
      this.tg.close();
    }
  }
}

export const telegram = new TelegramService();
