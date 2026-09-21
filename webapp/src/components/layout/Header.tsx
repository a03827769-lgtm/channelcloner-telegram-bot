import React from 'react';
import { Sparkles, Crown, Zap, Activity } from 'lucide-react';
import { User, Subscription } from '../../types';
import { telegram } from '../../services/telegram';

interface HeaderProps {
  user: User | null;
  subscription: Subscription | null;
  onOpenBilling: () => void;
  onOpenSystem?: () => void;
}

export const Header: React.FC<HeaderProps> = ({ user, subscription, onOpenBilling, onOpenSystem }) => {
  const isVip = subscription?.is_vip;
  const isPro = subscription?.tier === 'pro';

  return (
    <header className="w-full mt-1 mb-3 px-3.5 py-2 rounded-full vision-glass-panel flex items-center justify-between z-20 shrink-0 sticky top-1 animate-fade-up">
      {/* Left: visionOS Brand Pod */}
      <div className="flex items-center gap-2.5">
        <div className="relative">
          <div className="apple-squircle-badge bg-gradient-to-b from-[#1A8CFF] to-[#0071E3] w-8 h-8 rounded-[9px]">
            <Zap size={16} className="text-white fill-white" />
          </div>
          <span className="absolute -bottom-0.5 -right-0.5 w-2.5 h-2.5 bg-[#30D158] border-2 border-[#121317] rounded-full shadow-[0_0_8px_rgba(48,209,88,0.9)]" />
        </div>

        <div className="flex flex-col">
          <div className="flex items-center gap-1.5">
            <span className="text-[15px] font-bold tracking-[-0.03em] text-white">ChannelCloner</span>
            <span className="px-1.5 py-0.5 rounded-full text-[8.5px] font-extrabold tracking-wide uppercase bg-white/10 text-white/85 border border-white/10">
              {isVip ? 'VIP' : isPro ? 'PRO' : 'visionOS'}
            </span>
          </div>
          <div className="flex items-center gap-1 text-[10.5px] text-white/55 font-normal">
            <Activity size={10} className="text-[#30D158]" />
            <span>24/7 Sinxron</span>
          </div>
        </div>
      </div>

      {/* Right: Plan Status Capsule & Profile Avatar (From Image 2) */}
      <div className="flex items-center gap-2">
        <button
          onClick={() => {
            telegram.selection();
            onOpenBilling();
          }}
          className={`vision-btn-glass px-3 py-1.5 text-[12px] font-semibold flex items-center gap-1.5 active:scale-95 transition-transform ${
            isVip
              ? 'text-[#FFD60A] border-[#FFD60A]/40 bg-[#FFD60A]/15 shadow-[0_0_12px_rgba(255,214,10,0.25)]'
              : isPro
              ? 'text-[#64D2FF] border-[#0A84FF]/40 bg-[#0A84FF]/15 shadow-[0_0_12px_rgba(10,132,255,0.25)]'
              : 'text-white/80'
          }`}
        >
          {isVip ? (
            <>
              <Crown size={12} className="text-[#FFD60A]" />
              <span>VIP</span>
            </>
          ) : isPro ? (
            <>
              <Sparkles size={12} className="text-[#64D2FF]" />
              <span>Pro</span>
            </>
          ) : (
            <span>Sinov</span>
          )}
        </button>

        {/* User Profile Avatar Pill (Matching Image 2 top right) */}
        <button
          onClick={() => {
            if (onOpenSystem) {
              telegram.selection();
              onOpenSystem();
            }
          }}
          className="w-8 h-8 rounded-full vision-btn-glass flex items-center justify-center text-xs font-bold text-white/95 active:scale-95 transition-transform"
          title="Tizim holati"
        >
          <span>{user?.full_name ? user.full_name.charAt(0).toUpperCase() : 'U'}</span>
        </button>
      </div>
    </header>
  );
};
