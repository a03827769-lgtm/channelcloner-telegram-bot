import React, { useState, useEffect, useCallback } from 'react';
import {
  ActiveTab, User, Subscription, SummaryStats, ChannelPair, NewPairRequest, StorySettings, AudioTrack,
  StoryQueueItem, StoryPostedItem, SystemTelemetry, BillingCatalog
} from './types';
import { telegram } from './services/telegram';
import { api, errorText } from './services/api';
import { DeviceFrame } from './components/layout/DeviceFrame';
import { Header } from './components/layout/Header';
import { BottomNav } from './components/layout/BottomNav';
import { OverviewTab } from './components/dashboard/OverviewTab';
import { ChannelsTab } from './components/channels/ChannelsTab';
import { ChannelEditorModal } from './components/channels/ChannelEditorModal';
import { AddChannelModal } from './components/channels/AddChannelModal';
import { StoryStudioTab } from './components/story/StoryStudioTab';
import { StoreTab } from './components/store/StoreTab';
import { BackfillTab } from './components/backfill/BackfillTab';
import { BillingTab } from './components/billing/BillingTab';
import { SystemTab } from './components/system/SystemTab';
import { ToastContainer, ToastMessage } from './components/ui/Toast';
import { TelegramGuard } from './components/layout/TelegramGuard';

// Design preview with demo data (`?preview=1`) exists only in the Vite dev server; production builds
// always require a real Telegram session.
const PREVIEW_MODE =
  import.meta.env.DEV && typeof window !== 'undefined' && window.location.search.includes('preview=1');

const PREVIEW_PAIR: ChannelPair = {
  id: 101,
  user_id: 10001,
  source_channel: '@toshkent_news',
  source_title: 'Toshkent Yangiliklari',
  target_channel: '@mening_kanalim',
  target_title: 'Mening Tezkor Kanalim',
  is_active: true,
  clone_mode: 'clean',
  clean_links: true,
  custom_signature: "👉 @mening_kanalim ga obuna bo'ling!",
  remove_signature: false,
  blacklist_words: 'reklama, aksiya',
  replace_words: '',
  auto_translate: false,
  target_lang: 'uz',
  source_lang: 'auto',
  image_watermark_type: 'text',
  image_watermark_text: '@mening_kanalim',
  image_watermark_pos: 'bottom_right',
  video_watermark_type: 'none',
  video_watermark_text: '',
  video_watermark_pos: 'bottom_right',
  drip_delay_minutes: 5,
  night_mode: 'off',
  ai_paraphrase_mode: 'short',
  tone_of_voice: 'standard',
  ad_action: 'clean'
};

export const App: React.FC = () => {
  // Enforce Telegram-only access (decided before any hooks run, so hook order stays stable)
  const isTelegramEnv = telegram.isAvailable() || PREVIEW_MODE;

  if (!isTelegramEnv) {
    return <TelegramGuard />;
  }
  return <AppShell isPreview={PREVIEW_MODE} />;
};

const AppShell: React.FC<{ isPreview: boolean }> = ({ isPreview }) => {
  const [activeTab, setActiveTab] = useState<ActiveTab>('dashboard');
  const [user, setUser] = useState<User | null>(null);
  const [subscription, setSubscription] = useState<Subscription | null>(null);
  const [billing, setBilling] = useState<BillingCatalog | null>(null);
  const [stats, setStats] = useState<SummaryStats | null>(null);
  const [pairs, setPairs] = useState<ChannelPair[]>([]);
  const [storySettings, setStorySettings] = useState<StorySettings | null>(null);
  const [audioTracks, setAudioTracks] = useState<AudioTrack[]>([]);
  const [storyQueue, setStoryQueue] = useState<StoryQueueItem[]>([]);
  const [storyPosted, setStoryPosted] = useState<StoryPostedItem[]>([]);
  const [telemetry, setTelemetry] = useState<SystemTelemetry | null>(null);
  const [logs, setLogs] = useState<string[]>([]);

  // Modals & Sheets
  const [editingPair, setEditingPair] = useState<ChannelPair | null>(null);
  const [isAddModalOpen, setIsAddModalOpen] = useState<boolean>(false);
  const [toasts, setToasts] = useState<ToastMessage[]>([]);

  const isAdmin = Boolean(user?.is_admin);

  const addToast = useCallback((type: 'success' | 'error' | 'warning' | 'info', text: string) => {
    const id = Math.random().toString(36).substring(2, 9);
    setToasts((prev) => [...prev, { id, type, text }]);
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 4000);
  }, []);

  const dismissToast = (id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  };

  useEffect(() => {
    telegram.init();
  }, []);

  // Initial load (also used by the System tab refresh and after payments)
  const loadData = useCallback(async () => {
    try {
      const [
        meRes,
        pairsRes,
        storyRes,
        queueRes,
        tracksRes,
        sysRes
      ] = await Promise.allSettled([
        api.getMe(),
        api.getPairs(),
        api.getStorySettings(),
        api.getStoryQueue(),
        api.getAudioTracks(),
        api.getSystemStatus()
      ]);

      if (meRes.status === 'fulfilled') {
        setUser(meRes.value.user);
        setSubscription(meRes.value.subscription);
        setStats(meRes.value.stats);
        setBilling(meRes.value.billing ?? null);
      }

      if (pairsRes.status === 'fulfilled') {
        const fetched = pairsRes.value.pairs || [];
        // Preview demo pair so editor and channel tabs are fully inspectable
        setPairs(fetched.length === 0 && isPreview ? [PREVIEW_PAIR] : fetched);
      } else if (isPreview) {
        setPairs([PREVIEW_PAIR]);
      }

      if (storyRes.status === 'fulfilled') {
        setStorySettings(storyRes.value.settings);
      }

      if (queueRes.status === 'fulfilled') {
        setStoryQueue(queueRes.value.queue || []);
        setStoryPosted(queueRes.value.posted || []);
      }

      if (tracksRes.status === 'fulfilled') {
        setAudioTracks(tracksRes.value.tracks || []);
      }

      if (sysRes.status === 'fulfilled') {
        setTelemetry(sysRes.value.telemetry);
        setLogs(sysRes.value.logs || []);
      }
    } catch (err) {
      console.warn('Initial data load error:', err);
    }
  }, [isPreview]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  // Channel Operations
  /** Saves only the changed fields; errors propagate so the editor can show them and stay open. */
  const handleSavePair = async (pairId: number, changes: Partial<ChannelPair>) => {
    if (Object.keys(changes).length === 0) {
      addToast('info', "O'zgarish yo'q");
      return;
    }
    try {
      const res = await api.updatePair(pairId, changes);
      setPairs((prev) => prev.map((p) => (p.id === pairId ? res.pair : p)));
      addToast('success', 'Kanal sozlamalari muvaffaqiyatli saqlandi! ✨');
      telegram.notification('success');
    } catch (err) {
      if (!isPreview) {
        telegram.notification('error');
        throw err;
      }
      setPairs((prev) => prev.map((p) => (p.id === pairId ? { ...p, ...changes } : p)));
      addToast('success', 'Kanal sozlamalari saqlandi (Preview)! ✨');
    }
  };

  const handleTogglePair = async (id: number) => {
    // Optimistic UI update
    setPairs((prev) =>
      prev.map((p) => (p.id === id ? { ...p, is_active: !p.is_active } : p))
    );
    try {
      const res = await api.togglePair(id);
      setPairs((prev) =>
        prev.map((p) => (p.id === id ? { ...p, is_active: res.is_active } : p))
      );
      addToast('info', res.is_active ? 'Kanal faollashtirildi' : "Kanal to'xtatildi");
    } catch (err) {
      if (!isPreview) {
        setPairs((prev) =>
          prev.map((p) => (p.id === id ? { ...p, is_active: !p.is_active } : p))
        );
        addToast('error', errorText(err, "Holatni o'zgartirib bo'lmadi"));
        telegram.notification('error');
      }
    }
  };

  const handleDeletePair = async (id: number) => {
    try {
      await api.deletePair(id);
      setPairs((prev) => prev.filter((p) => p.id !== id));
      addToast('success', "Kanal juftligi o'chirildi");
      telegram.notification('success');
    } catch (err) {
      if (isPreview) {
        setPairs((prev) => prev.filter((p) => p.id !== id));
        addToast('success', "Kanal juftligi o'chirildi (Preview)");
        telegram.notification('success');
      } else {
        addToast('error', errorText(err, "O'chirishda xatolik"));
      }
    }
  };

  const handleAddPair = async (data: NewPairRequest) => {
    // Errors propagate to the modal so the user sees the server's validation message
    const res = await api.createPair(data);
    const created = res.pair;
    if (created) {
      setPairs((prev) => [created, ...prev.filter((p) => p.id !== created.id)]);
    }
    addToast('success', 'Yangi kanal juftligi muvaffaqiyatli ulandi! 🚀');
    telegram.notification('success');
  };

  const handleTestPost = async (id: number) => {
    try {
      const res = await api.sendTestPost(id);
      addToast('success', res.message || 'Sinov xabari yuborildi!');
      telegram.notification('success');
    } catch (err) {
      if (isPreview) {
        addToast('success', 'Sinov xabari yuborildi (Simulyatsiya)!');
        telegram.notification('success');
      } else {
        addToast('error', errorText(err, 'Sinov xabarida xatolik'));
        telegram.notification('error');
      }
    }
  };

  const handleTriggerQuickTest = async () => {
    if (pairs.length === 0) {
      addToast('warning', 'Sinov xabari yuborish uchun avval kanal ulang');
      return;
    }
    await handleTestPost(pairs[0].id);
  };

  // Story Settings
  const handleSaveStorySettings = async (changes: Partial<StorySettings>) => {
    if (Object.keys(changes).length === 0) {
      addToast('info', "O'zgarish yo'q");
      return;
    }
    try {
      const res = await api.saveStorySettings(changes);
      setStorySettings(res.settings);
      addToast('success', 'VIP Istoriya sozlamalari saqlandi! 🎬');
      telegram.notification('success');
    } catch (err) {
      if (isPreview) {
        setStorySettings((prev) => (prev ? { ...prev, ...changes } : prev));
        addToast('success', 'VIP Istoriya sozlamalari saqlandi (Preview)! 🎬');
        telegram.notification('success');
      } else {
        addToast('error', errorText(err, 'Saqlashda xatolik'));
        telegram.notification('error');
      }
    }
  };

  // Backfill (errors propagate to the Backfill tab log)
  const handleTriggerBackfill = async (pairId: number, count: number) => {
    try {
      const res = await api.triggerBackfill(pairId, count);
      addToast('info', res.message || `${count} ta postni ko'chirish navbatga qo'yildi`);
    } catch (err) {
      if (!isPreview) {
        addToast('error', errorText(err, 'Xatolik yuz berdi'));
        throw err;
      }
      addToast('info', `${count} ta postni ko'chirish navbatga qo'yildi (Simulyatsiya)`);
    }
  };

  // Billing
  const handleSelectPlan = async (planKey: string) => {
    telegram.impact('medium');
    if (planKey !== 'pro' && planKey !== 'vip') {
      if (subscription?.is_trial_active) {
        addToast('info', 'Siz allaqachon bepul sinovdasiz!');
      } else {
        addToast('warning', "Sinov muddati tugagan. Davom etish uchun Pro yoki VIP tarifini tanlang.");
      }
      return;
    }

    try {
      const res = await api.checkout(planKey);
      telegram.openInvoice(res.invoice_link, (status) => {
        if (status === 'paid') {
          addToast('success', "To'lov muvaffaqiyatli amalga oshirildi! 🎉");
          telegram.notification('success');
          loadData();
        } else if (status === 'failed') {
          addToast('error', "To'lov amalga oshmadi");
        }
      });
    } catch (err) {
      if (isPreview) {
        addToast('info', `Telegram Stars to'lov darchasi (Preview: ${planKey.toUpperCase()})`);
      } else {
        addToast('error', errorText(err, "To'lov havolasini olishda xatolik"));
      }
    }
  };

  return (
    <DeviceFrame>
      <div className="ios26-ambient-glow" />

      {/* Toast Alert Notifications */}
      <ToastContainer toasts={toasts} onDismiss={dismissToast} />

      {/* Main Top Header */}
      <Header
        user={user}
        subscription={subscription}
        onOpenBilling={() => setActiveTab('billing')}
        onOpenSystem={() => setActiveTab('system')}
      />

      {/* Screen Body Router */}
      <main className="flex-1 w-full overflow-y-auto pb-28">
        {activeTab === 'dashboard' && (
          <OverviewTab
            user={user}
            subscription={subscription}
            stats={stats}
            onNavigate={(tab) => setActiveTab(tab)}
            onOpenAddModal={() => setIsAddModalOpen(true)}
            onTriggerTestPost={handleTriggerQuickTest}
          />
        )}

        {activeTab === 'channels' && (
          <ChannelsTab
            pairs={pairs}
            onOpenAddModal={() => setIsAddModalOpen(true)}
            onSelectPair={(pair) => setEditingPair(pair)}
            onTogglePair={handleTogglePair}
          />
        )}

        {activeTab === 'story' && (
          <StoryStudioTab
            settings={storySettings}
            audioTracks={audioTracks}
            queue={storyQueue}
            posted={storyPosted}
            isVip={Boolean(subscription?.is_vip)}
            onSaveSettings={handleSaveStorySettings}
            onOpenBilling={() => setActiveTab('billing')}
          />
        )}

        {activeTab === 'store' && <StoreTab />}

        {activeTab === 'backfill' && (
          <BackfillTab
            pairs={pairs}
            onTriggerBackfill={handleTriggerBackfill}
          />
        )}

        {activeTab === 'billing' && (
          <BillingTab
            subscription={subscription}
            billing={billing}
            onSelectPlan={handleSelectPlan}
          />
        )}

        {activeTab === 'system' && (
          <SystemTab
            telemetry={telemetry}
            logs={logs}
            isAdmin={isAdmin}
            onRefresh={loadData}
          />
        )}
      </main>

      {/* Floating Bottom Navigation Bar */}
      <BottomNav
        activeTab={activeTab}
        onChangeTab={(tab) => setActiveTab(tab)}
        isVip={Boolean(subscription?.is_vip)}
      />

      {/* Deep Channel Inspector Modal */}
      {editingPair && (
        <ChannelEditorModal
          pair={editingPair}
          subscription={subscription}
          isAdmin={isAdmin}
          onClose={() => setEditingPair(null)}
          onSave={handleSavePair}
          onDelete={handleDeletePair}
          onTestPost={handleTestPost}
          onOpenBilling={() => {
            setEditingPair(null);
            setActiveTab('billing');
          }}
        />
      )}

      {/* 3-Step Wizard for Adding Channel */}
      {isAddModalOpen && (
        <AddChannelModal
          onClose={() => setIsAddModalOpen(false)}
          onAdd={handleAddPair}
        />
      )}
    </DeviceFrame>
  );
};

export default App;
