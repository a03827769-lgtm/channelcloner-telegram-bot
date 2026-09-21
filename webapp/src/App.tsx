import React, { useState, useEffect, useCallback } from 'react';
import { ActiveTab, User, Subscription, SummaryStats, ChannelPair, StorySettings, AudioTrack, StoryQueueItem, SystemTelemetry } from './types';
import { telegram } from './services/telegram';
import { api } from './services/api';
import { DeviceFrame } from './components/layout/DeviceFrame';
import { Header } from './components/layout/Header';
import { BottomNav } from './components/layout/BottomNav';
import { OverviewTab } from './components/dashboard/OverviewTab';
import { ChannelsTab } from './components/channels/ChannelsTab';
import { ChannelEditorModal } from './components/channels/ChannelEditorModal';
import { AddChannelModal } from './components/channels/AddChannelModal';
import { StoryStudioTab } from './components/story/StoryStudioTab';
import { BackfillTab } from './components/backfill/BackfillTab';
import { BillingTab } from './components/billing/BillingTab';
import { SystemTab } from './components/system/SystemTab';
import { ToastContainer, ToastMessage } from './components/ui/Toast';
import { TelegramGuard } from './components/layout/TelegramGuard';

export const App: React.FC = () => {
  // Enforce Telegram-only access
  const isPreview = typeof window !== 'undefined' && window.location.search.includes('preview=1');
  const isTelegramEnv = telegram.isAvailable() || isPreview;

  if (!isTelegramEnv) {
    return <TelegramGuard />;
  }
  const [activeTab, setActiveTab] = useState<ActiveTab>('dashboard');
  const [user, setUser] = useState<User | null>(null);
  const [subscription, setSubscription] = useState<Subscription | null>(null);
  const [stats, setStats] = useState<SummaryStats | null>(null);
  const [pairs, setPairs] = useState<ChannelPair[]>([]);
  const [storySettings, setStorySettings] = useState<StorySettings | null>(null);
  const [audioTracks, setAudioTracks] = useState<AudioTrack[]>([]);
  const [storyQueue, setStoryQueue] = useState<StoryQueueItem[]>([]);
  const [storyPosted, setStoryPosted] = useState<any[]>([]);
  const [telemetry, setTelemetry] = useState<SystemTelemetry | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  
  // Modals & Sheets
  const [editingPair, setEditingPair] = useState<ChannelPair | null>(null);
  const [isAddModalOpen, setIsAddModalOpen] = useState<boolean>(false);
  const [toasts, setToasts] = useState<ToastMessage[]>([]);

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

  // Initial load
  const loadData = useCallback(async () => {
    try {
      telegram.init();
      
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
      }

      if (pairsRes.status === 'fulfilled') {
        const fetched = pairsRes.value.pairs || [];
        if (fetched.length === 0 && isPreview) {
          // Preview demo pair so editor and channel tabs are fully inspectable
          setPairs([
            {
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
            }
          ]);
        } else {
          setPairs(fetched);
        }
      } else if (isPreview) {
        setPairs([
          {
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
          }
        ]);
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

    } catch (err: any) {
      console.warn('Initial data load error:', err);
    }
  }, [isPreview]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  // Channel Operations
  const handleSavePair = async (updated: ChannelPair) => {
    try {
      await api.updatePair(updated.id, updated);
      setPairs((prev) => prev.map((p) => (p.id === updated.id ? updated : p)));
      addToast('success', 'Kanal sozlamalari muvaffaqiyatli saqlandi! ✨');
      telegram.notification('success');
    } catch (err: any) {
      if (isPreview) {
        setPairs((prev) => prev.map((p) => (p.id === updated.id ? updated : p)));
        addToast('success', 'Kanal sozlamalari saqlandi (Preview)! ✨');
        telegram.notification('success');
      } else {
        addToast('error', err.message || 'Saqlashda xatolik');
        telegram.notification('error');
      }
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
    } catch (err: any) {
      if (!isPreview) {
        setPairs((prev) =>
          prev.map((p) => (p.id === id ? { ...p, is_active: !p.is_active } : p))
        );
        addToast('error', err.message || 'Holatni o\'zgartirib bo\'lmadi');
      }
    }
  };

  const handleDeletePair = async (id: number) => {
    try {
      await api.deletePair(id);
      setPairs((prev) => prev.filter((p) => p.id !== id));
      addToast('success', 'Kanal juftligi o\'chirildi');
      telegram.notification('success');
    } catch (err: any) {
      if (isPreview) {
        setPairs((prev) => prev.filter((p) => p.id !== id));
        addToast('success', 'Kanal juftligi o\'chirildi (Preview)');
        telegram.notification('success');
      } else {
        addToast('error', err.message || 'O\'chirishda xatolik');
      }
    }
  };

  const handleAddPair = async (data: Partial<ChannelPair>) => {
    let newId = Date.now();
    try {
      const res = await api.createPair(data);
      if (res.pair_id) newId = res.pair_id;
    } catch (err: any) {
      if (!isPreview) throw err;
    }

    const newPair: ChannelPair = {
      id: newId,
      user_id: user?.id || 10001,
      source_channel: data.source_channel || '',
      source_title: data.source_title || data.source_channel || '',
      target_channel: data.target_channel || '',
      target_title: data.target_title || data.target_channel || '',
      is_active: true,
      clone_mode: data.clone_mode || 'clean',
      clean_links: data.clean_links ?? true,
      custom_signature: '',
      remove_signature: false,
      blacklist_words: '',
      replace_words: '',
      auto_translate: data.auto_translate ?? false,
      target_lang: 'uz',
      source_lang: 'auto',
      image_watermark_type: 'none',
      image_watermark_text: '',
      image_watermark_pos: 'bottom_right',
      video_watermark_type: 'none',
      video_watermark_text: '',
      video_watermark_pos: 'bottom_right',
      drip_delay_minutes: 0,
      night_mode: 'off',
      ai_paraphrase_mode: 'off',
      tone_of_voice: 'standard',
      ad_action: 'clean'
    };
    setPairs((prev) => [newPair, ...prev]);
    addToast('success', 'Yangi kanal juftligi muvaffaqiyatli ulandi! 🚀');
    telegram.notification('success');
  };

  const handleTestPost = async (id: number) => {
    try {
      const res = await api.sendTestPost(id);
      addToast('success', res.message || 'Sinov xabari yuborildi!');
      telegram.notification('success');
    } catch (err: any) {
      if (isPreview) {
        addToast('success', 'Sinov xabari yuborildi (Simulyatsiya)!');
        telegram.notification('success');
      } else {
        addToast('error', err.message || 'Sinov xabarida xatolik');
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
  const handleSaveStorySettings = async (updated: Partial<StorySettings>) => {
    try {
      await api.saveStorySettings(updated);
      setStorySettings((prev) => (prev ? { ...prev, ...updated } : (updated as StorySettings)));
      addToast('success', 'VIP Istoriya sozlamalari saqlandi! 🎬');
      telegram.notification('success');
    } catch (err: any) {
      if (isPreview) {
        setStorySettings((prev) => (prev ? { ...prev, ...updated } : (updated as StorySettings)));
        addToast('success', 'VIP Istoriya sozlamalari saqlandi (Preview)! 🎬');
        telegram.notification('success');
      } else {
        addToast('error', err.message || 'Saqlashda xatolik');
        telegram.notification('error');
      }
    }
  };

  // Backfill
  const handleTriggerBackfill = async (pairId: number, count: number) => {
    try {
      await api.triggerBackfill(pairId, count);
      addToast('info', `${count} ta postni ko'chirish navbatga qo'yildi`);
    } catch (err: any) {
      if (isPreview) {
        addToast('info', `${count} ta postni ko'chirish navbatga qo'yildi (Simulyatsiya)`);
      } else {
        addToast('error', err.message || 'Xatolik yuz berdi');
      }
    }
  };

  // Billing
  const handleSelectPlan = async (planKey: string) => {
    telegram.impact('medium');
    if (planKey === 'free') {
      addToast('info', 'Siz allaqachon bepul sinovdasiz!');
      return;
    }
    
    try {
      const res = await api.checkout(planKey);
      telegram.openInvoice(res.invoice_link, (status) => {
        if (status === 'paid') {
          addToast('success', 'To\'lov muvaffaqiyatli amalga oshirildi! 🎉');
          telegram.notification('success');
          loadData();
        } else if (status === 'failed') {
          addToast('error', 'To\'lov amalga oshmadi');
        }
      });
    } catch (err: any) {
      if (isPreview) {
        addToast('info', `Telegram Stars to'lov darchasi (Preview: ${planKey.toUpperCase()})`);
      } else {
        addToast('error', err.message || "To'lov havolasini olishda xatolik");
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

        {activeTab === 'backfill' && (
          <BackfillTab
            pairs={pairs}
            onTriggerBackfill={handleTriggerBackfill}
          />
        )}

        {activeTab === 'billing' && (
          <BillingTab
            subscription={subscription}
            onSelectPlan={handleSelectPlan}
          />
        )}

        {activeTab === 'system' && (
          <SystemTab
            telemetry={telemetry}
            logs={logs}
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
          onClose={() => setEditingPair(null)}
          onSave={handleSavePair}
          onDelete={handleDeletePair}
          onTestPost={handleTestPost}
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
