import React, { useState, useEffect } from 'react';
import { Layers, Radio, RefreshCw, Crown, ShieldCheck, CheckCircle2, PlayCircle, Plus, Clapperboard, BarChart3, TrendingUp } from 'lucide-react';
import { telegram } from '../../services/telegram';
import { User, Subscription, SummaryStats } from '../../types';
import { api } from '../../services/api';

interface OverviewTabProps {
  user: User | null;
  subscription: Subscription | null;
  stats: SummaryStats | null;
  onNavigate: (tab: any) => void;
  onOpenAddModal: () => void;
  onTriggerTestPost?: () => Promise<void>;
}

export const OverviewTab: React.FC<OverviewTabProps> = ({ 
  user, 
  subscription, 
  stats, 
  onNavigate, 
  onOpenAddModal,
  onTriggerTestPost
}) => {
  const [feed, setFeed] = useState<any[]>([]);
  const [isTesting, setIsTesting] = useState(false);
  const [chartPeriod, setChartPeriod] = useState<'24h' | '7d'>('7d');
  const isVip = subscription?.is_vip;

  useEffect(() => {
    let mounted = true;
    const fetchFeed = async () => {
      try {
        const res = await api.getFeed();
        if (mounted && res.feed) {
          setFeed(res.feed || []);
        }
      } catch (err) {
        // Silently catch polling errors
      }
    };

    fetchFeed();
    const interval = setInterval(fetchFeed, 6000);
    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, []);

  const handleTestBot = async () => {
    telegram.impact('medium');
    setIsTesting(true);
    try {
      if (onTriggerTestPost) {
        await onTriggerTestPost();
      } else {
        await new Promise((r) => setTimeout(r, 1000));
        telegram.notification('success');
      }
    } finally {
      setIsTesting(false);
    }
  };

  // 7-day activity simulation (from Image 1 visionOS chart)
  const chartDays = [
    { day: 'Dush', count: 42, height: '55%' },
    { day: 'Sesh', count: 68, height: '75%' },
    { day: 'Chor', count: 95, height: '95%', isPeak: true },
    { day: 'Pay',  count: 55, height: '65%' },
    { day: 'Jum',  count: 82, height: '85%' },
    { day: 'Shan', count: 40, height: '50%' },
    { day: 'Yak',  count: 60, height: '70%' },
  ];

  return (
    <div className="flex flex-col gap-4">
      
      {/* visionOS Hero Glass Panel (Modeled after Image 1 "Hola, Azzahri Alpiana 👋") */}
      <div className="vision-glass-panel p-5 relative overflow-hidden flex flex-col gap-4 animate-fade-up">
        {/* Subtle ambient light reflection */}
        <div className="absolute -top-12 -right-12 w-44 h-44 bg-gradient-to-br from-[#0A84FF]/25 via-[#BF5AF2]/15 to-transparent rounded-full blur-3xl pointer-events-none" />
        <div className="absolute top-0 inset-x-0 h-px bg-gradient-to-r from-transparent via-white/25 to-transparent" />

        <div className="flex items-start justify-between relative z-10">
          <div className="flex flex-col gap-1">
            <div className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full bg-emerald-500/15 border border-emerald-500/25 text-[11px] font-semibold text-[#30D158] w-fit">
              <span className="w-1.5 h-1.5 rounded-full bg-[#30D158] animate-pulse" />
              <span>Avtomatik Klonlash 24/7 Faol</span>
            </div>
            <h2 className="text-[22px] font-bold text-white tracking-[-0.025em] mt-1 flex items-center gap-2">
              <span>Assalomu alaykum, {user?.full_name?.split(' ')[0] || 'Foydalanuvchi'}!</span>
              <span className="text-xl">👋</span>
            </h2>
            <p className="text-[13px] text-white/65 leading-relaxed font-normal max-w-sm">
              Kanallarni avtomatik ko'chirish, reklamalarni tozalash va 24/7 uzluksiz sinxronizatsiya.
            </p>
          </div>
        </div>

        {/* Action Buttons Row */}
        <div className="grid grid-cols-2 gap-2.5 relative z-10 pt-1">
          <button
            type="button"
            onClick={() => {
              telegram.impact('medium');
              onOpenAddModal();
            }}
            className="vision-btn-blue py-3 px-4 flex items-center justify-center gap-2 text-[13px] font-semibold tracking-tight active:scale-[0.96] transition-transform"
          >
            <Plus size={16} strokeWidth={2.5} />
            <span>Kanal Ulash</span>
          </button>

          <button
            type="button"
            onClick={() => {
              telegram.selection();
              onNavigate('story');
            }}
            className="vision-btn-glass py-3 px-4 flex items-center justify-center gap-2 text-[13px] font-semibold tracking-tight text-white/90 active:scale-[0.96] transition-transform"
          >
            <Clapperboard size={15} className="text-[#FFD60A]" />
            <span>VIP Story Studio</span>
          </button>
        </div>
      </div>

      {/* 4 visionOS Stat Pods with Colored Squircles (From Image 1 & 4) */}
      <div className="grid grid-cols-2 gap-3">
        {/* Metric 1: Total Cloned */}
        <div className="vision-card p-4 flex flex-col justify-between min-h-[110px] relative overflow-hidden animate-fade-up stagger-1">
          <div className="flex items-center justify-between">
            <span className="text-white/60 text-[11px] font-semibold tracking-wide uppercase">
              Ko'chirilgan
            </span>
            <div className="apple-squircle-badge bg-[#30D158] w-7 h-7 rounded-[8px]">
              <Layers size={13} className="text-white" />
            </div>
          </div>
          <div className="mt-2">
            <div className="text-[28px] font-bold text-white tracking-tight leading-none tabular-nums font-mono">
              {stats?.total_cloned_messages ?? 0}
            </div>
            <div className="text-[11px] text-[#30D158] font-medium mt-1 flex items-center gap-1">
              <TrendingUp size={11} />
              <span>Jami xabarlar</span>
            </div>
          </div>
        </div>

        {/* Metric 2: Active Pairs */}
        <div className="vision-card p-4 flex flex-col justify-between min-h-[110px] relative overflow-hidden animate-fade-up stagger-2">
          <div className="flex items-center justify-between">
            <span className="text-white/60 text-[11px] font-semibold tracking-wide uppercase">
              Juftliklar
            </span>
            <div className="apple-squircle-badge bg-[#0A84FF] w-7 h-7 rounded-[8px]">
              <Radio size={13} className="text-white" />
            </div>
          </div>
          <div className="mt-2">
            <div className="text-[28px] font-bold text-white tracking-tight leading-none flex items-baseline gap-1 tabular-nums font-mono">
              {stats?.channel_pairs_count ?? 0}
              <span className="text-xs text-white/40 font-normal">/ {subscription?.max_channels ?? 1}</span>
            </div>
            <div className="text-[11px] text-[#0A84FF] font-medium mt-1">
              Limit va Holat
            </div>
          </div>
        </div>

        {/* Metric 3: VIP Story Cloner */}
        <div className="vision-card p-4 flex flex-col justify-between min-h-[110px] relative overflow-hidden animate-fade-up stagger-3">
          <div className="flex items-center justify-between">
            <span className="text-white/60 text-[11px] font-semibold tracking-wide uppercase">
              VIP Istoriya
            </span>
            <div className="apple-squircle-badge bg-[#FFD60A] w-7 h-7 rounded-[8px] text-black">
              <Crown size={13} className="text-black fill-black" />
            </div>
          </div>
          <div className="mt-2">
            <div className="text-[28px] font-bold text-[#FFD60A] tracking-tight leading-none">
              {subscription?.is_vip ? 'Faol' : 'VIP'}
            </div>
            <div className="text-[11px] text-white/50 font-medium mt-1">
              {subscription?.is_vip ? '4K Ken Burns rejimi' : 'Maksimal imkoniyat'}
            </div>
          </div>
        </div>

        {/* Metric 4: Success Rate */}
        <div className="vision-card p-4 flex flex-col justify-between min-h-[110px] relative overflow-hidden animate-fade-up stagger-4">
          <div className="flex items-center justify-between">
            <span className="text-white/60 text-[11px] font-semibold tracking-wide uppercase">
              Ishonchlilik
            </span>
            <div className="apple-squircle-badge bg-[#64D2FF] w-7 h-7 rounded-[8px]">
              <ShieldCheck size={13} className="text-white" />
            </div>
          </div>
          <div className="mt-2">
            <div className="text-[28px] font-bold text-white tracking-tight leading-none tabular-nums font-mono">
              {stats?.success_rate ?? 99.8}%
            </div>
            <div className="text-[11px] text-white/50 font-medium mt-1">
              Zero-Loss Kafolati
            </div>
          </div>
        </div>
      </div>

      {/* visionOS 7-Day Activity Bar Chart (Directly from Image 1!) */}
      <div className="vision-glass-panel p-4 flex flex-col gap-3 animate-fade-up stagger-5">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <BarChart3 size={15} className="text-[#0A84FF]" />
            <span className="text-[13px] font-semibold text-white">Haftalik Faollik Grafigi</span>
          </div>

          <div className="flex items-center p-0.5 rounded-full bg-white/[0.08] border border-white/10 text-[10px]">
            <button
              onClick={() => {
                telegram.selection();
                setChartPeriod('24h');
              }}
              className={`px-2.5 py-0.5 rounded-full transition-all ${
                chartPeriod === '24h' ? 'bg-white/25 text-white font-bold' : 'text-white/50'
              }`}
            >
              24h
            </button>
            <button
              onClick={() => {
                telegram.selection();
                setChartPeriod('7d');
              }}
              className={`px-2.5 py-0.5 rounded-full transition-all ${
                chartPeriod === '7d' ? 'bg-white/25 text-white font-bold' : 'text-white/50'
              }`}
            >
              7 kun
            </button>
          </div>
        </div>

        {/* Blue Pill Bars (Exactly like Image 1 center graph) */}
        <div className="relative h-32 pt-4 px-2">
          {/* Subtle horizontal reference grid lines from Image 1 */}
          <div className="absolute inset-x-2 top-6 border-b border-white/[0.05]" />
          <div className="absolute inset-x-2 top-16 border-b border-white/[0.05]" />
          <div className="absolute inset-x-2 top-24 border-b border-white/[0.05]" />

          <div className="relative z-10 flex items-end justify-between gap-2 h-full">
            {chartDays.map((item, idx) => (
              <div key={idx} className="flex-1 flex flex-col items-center gap-1.5 h-full justify-end group">
                <div className="w-full max-w-[24px] flex flex-col justify-end h-[78px] relative">
                  {/* Background recessed slot */}
                  <div className="w-full rounded-full bg-white/[0.05] absolute inset-0" />
                  {/* Pill Bar: Active Apple Blue for Peak, Smoky Glass for other days (Image 1) */}
                  <div
                    className={`w-full rounded-full relative z-10 transition-all duration-500 ${
                      item.isPeak
                        ? 'bg-gradient-to-t from-[#0071E3] to-[#0A84FF] shadow-[0_0_14px_rgba(10,132,255,0.85)] border border-[#64D2FF]/40'
                        : 'bg-white/[0.18] group-hover:bg-white/[0.28]'
                    }`}
                    style={{ height: item.height }}
                  />
                </div>
                <span className={`text-[10px] font-mono ${item.isPeak ? 'text-[#64D2FF] font-bold' : 'text-white/45'}`}>
                  {item.day}
                </span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Horizontal Action Pills */}
      <div className="flex items-center gap-2 overflow-x-auto pb-1 pt-0.5 no-scrollbar animate-fade-up stagger-6">
        <button
          onClick={() => {
            telegram.selection();
            onNavigate('channels');
          }}
          className="vision-btn-glass px-3.5 py-2 text-[12px] font-medium text-white/85 flex items-center gap-1.5 whitespace-nowrap active:scale-95 transition-transform"
        >
          <Radio size={13} className="text-[#0A84FF]" />
          <span>Kanallar ro'yxati</span>
        </button>

        <button
          onClick={() => {
            telegram.selection();
            onNavigate('backfill');
          }}
          className="vision-btn-glass px-3.5 py-2 text-[12px] font-medium text-white/85 flex items-center gap-1.5 whitespace-nowrap active:scale-95 transition-transform"
        >
          <RefreshCw size={13} className="text-[#64D2FF]" />
          <span>Tarixni ko'chirish</span>
        </button>

        <button
          disabled={isTesting}
          onClick={handleTestBot}
          className="vision-btn-glass px-3.5 py-2 text-[12px] font-medium text-white/85 flex items-center gap-1.5 whitespace-nowrap active:scale-95 transition-transform disabled:opacity-50"
        >
          {isTesting ? <RefreshCw size={13} className="animate-spin text-[#30D158]" /> : <PlayCircle size={13} className="text-[#30D158]" />}
          <span>{isTesting ? 'Sinovda...' : 'Botni sinash'}</span>
        </button>

        <button
          onClick={() => {
            telegram.selection();
            onNavigate('billing');
          }}
          className="vision-btn-glass px-3.5 py-2 text-[12px] font-medium text-[#FFD60A] flex items-center gap-1.5 whitespace-nowrap active:scale-95 transition-transform border-[#FFD60A]/30 bg-[#FFD60A]/10"
        >
          <Crown size={13} className="text-[#FFD60A]" />
          <span>Tariflar</span>
        </button>
      </div>

      {/* visionOS Grouped Inset Live Activity Feed (Matching Image 1 & 4) */}
      <div className="flex flex-col gap-2">
        <div className="flex items-center justify-between px-1">
          <span className="text-[12px] font-semibold text-white/65 tracking-wider uppercase flex items-center gap-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-[#30D158] animate-pulse" />
            Jonli Voqealar Lentasi
          </span>
          <span className="text-[11px] text-white/45 font-normal">So'nggi harakatlar</span>
        </div>

        <div className="vision-grouped-list p-1 flex flex-col animate-fade-up stagger-7">
          {feed.length === 0 ? (
            <div className="text-center py-7 text-white/45 text-xs font-normal">
              Hozircha faol ko'chirilgan postlar yo'q. Yangi postlar chiqqanda shu yerda avtomatik paydo bo'ladi.
            </div>
          ) : (
            feed.map((item, idx) => (
              <React.Fragment key={item.id || idx}>
                {idx > 0 && <div className="vision-hairline" />}
                <div className="flex items-center gap-3 p-3 rounded-[18px] hover:bg-white/[0.04] transition-colors">
                  <div className="apple-squircle-badge bg-[#30D158]/20 border border-[#30D158]/30 w-8 h-8 rounded-[9px]">
                    <CheckCircle2 size={16} className="text-[#30D158]" />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center justify-between">
                      <span className="font-semibold text-white text-[13px] truncate mr-2">
                        {item.media_type === 'story' ? 'VIP Istoriya' : 'Post Ko\'chirildi'}
                      </span>
                      <span className="text-[10px] text-white/40 whitespace-nowrap font-mono tabular-nums">
                        {new Date(item.cloned_at).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})}
                      </span>
                    </div>
                    <p className="text-[11px] text-white/50 truncate mt-0.5 font-mono">
                      {item.cp_src} &rarr; {item.cp_tgt}
                    </p>
                  </div>
                </div>
              </React.Fragment>
            ))
          )}
        </div>
      </div>
      
    </div>
  );
};
