import React from 'react';
import { LayoutGrid, Radio, Clapperboard, History } from 'lucide-react';
import { ActiveTab } from '../../types';
import { telegram } from '../../services/telegram';

interface BottomNavProps {
  activeTab: ActiveTab;
  onChangeTab: (tab: ActiveTab) => void;
  isVip: boolean;
}

export const BottomNav: React.FC<BottomNavProps> = ({ activeTab, onChangeTab, isVip }) => {
  const tabs = [
    { id: 'dashboard' as ActiveTab, label: 'Asosiy', icon: LayoutGrid },
    { id: 'channels' as ActiveTab, label: 'Kanallar', icon: Radio },
    { id: 'story' as ActiveTab, label: 'VIP Story', icon: Clapperboard, isHighlight: true },
    { id: 'backfill' as ActiveTab, label: 'Tarix', icon: History },
  ];

  return (
    <div 
      className="fixed inset-x-0 max-w-[344px] mx-auto z-40 pointer-events-none px-3"
      style={{
        bottom: 'calc(env(safe-area-inset-bottom, 0px) + 12px)'
      }}
    >
      <nav className="pointer-events-auto vision-dock py-1.5 px-2 flex items-center justify-between">
        {tabs.map((tab) => {
          const Icon = tab.icon;
          const isActive = activeTab === tab.id;

          return (
            <button
              key={tab.id}
              type="button"
              onClick={() => {
                telegram.selection();
                onChangeTab(tab.id);
              }}
              style={{ transitionTimingFunction: 'cubic-bezier(0.34, 1.56, 0.64, 1)' }}
              className={`relative flex-1 min-h-[46px] py-1 px-1 rounded-full flex flex-col items-center justify-center transition-all duration-300 active:scale-[0.88] ${
                isActive
                  ? 'text-white'
                  : 'text-white/50 hover:text-white/80'
              }`}
            >
              {/* Active Tab Liquid Capsule Pill */}
              {isActive && (
                <div
                  style={{ transitionTimingFunction: 'cubic-bezier(0.34, 1.56, 0.64, 1)' }}
                  className={`absolute inset-0 rounded-full transition-all duration-350 ${
                    tab.isHighlight
                      ? 'bg-[#FFD60A]/20 border border-[#FFD60A]/40 shadow-[0_0_14px_rgba(255,214,10,0.35),inset_0_1px_0_rgba(255,255,255,0.4)]'
                      : 'bg-white/[0.22] border border-white/28 shadow-[0_3px_12px_rgba(0,0,0,0.30),inset_0_0.5px_0_rgba(255,255,255,0.50)]'
                  }`}
                />
              )}

              {/* VIP Indicator dot */}
              {tab.isHighlight && !isVip && (
                <span className="absolute top-1 right-3 w-1.5 h-1.5 rounded-full bg-[#FFD60A] shadow-[0_0_8px_rgba(255,214,10,1)]" />
              )}

              <Icon
                size={19}
                className={`relative z-10 transition-transform duration-200 ${
                  isActive
                    ? tab.isHighlight
                      ? 'text-[#FFD60A] scale-105'
                      : 'text-white scale-105'
                    : ''
                }`}
                strokeWidth={isActive ? 2.4 : 1.8}
              />
              <span
                className={`relative z-10 text-[10px] mt-0.5 font-medium tracking-tight transition-colors duration-200 ${
                  isActive
                    ? tab.isHighlight
                      ? 'text-[#FFD60A] font-semibold'
                      : 'text-white font-semibold'
                    : ''
                }`}
              >
                {tab.label}
              </span>
            </button>
          );
        })}
      </nav>
    </div>
  );
};
