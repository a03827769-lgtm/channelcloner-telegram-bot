import React, { useState } from 'react';
import { Plus, Search, Radio, Sliders, ArrowRight, X, ShieldCheck, Globe, Send, Image as ImageIcon, Check, Mic } from 'lucide-react';
import { ChannelPair } from '../../types';
import { Switch } from '../ui/Switch';
import { telegram } from '../../services/telegram';

interface ChannelsTabProps {
  pairs: ChannelPair[];
  onOpenAddModal: () => void;
  onSelectPair: (pair: ChannelPair) => void;
  onTogglePair: (id: number) => Promise<void>;
}

export const ChannelsTab: React.FC<ChannelsTabProps> = ({
  pairs,
  onOpenAddModal,
  onSelectPair,
  onTogglePair,
}) => {
  const [searchQuery, setSearchQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState<'all' | 'active' | 'paused'>('all');
  const [copiedChannel, setCopiedChannel] = useState<string | null>(null);

  const handleCopyChannel = (channel: string, e: React.MouseEvent) => {
    e.stopPropagation();
    navigator.clipboard?.writeText(channel);
    setCopiedChannel(channel);
    telegram.impact('light');
    setTimeout(() => setCopiedChannel(null), 2000);
  };

  const filteredPairs = pairs.filter((pair) => {
    const matchesSearch =
      pair.source_channel.toLowerCase().includes(searchQuery.toLowerCase()) ||
      pair.target_channel.toLowerCase().includes(searchQuery.toLowerCase()) ||
      (pair.source_title && pair.source_title.toLowerCase().includes(searchQuery.toLowerCase())) ||
      (pair.target_title && pair.target_title.toLowerCase().includes(searchQuery.toLowerCase()));

    if (statusFilter === 'active') return matchesSearch && pair.is_active;
    if (statusFilter === 'paused') return matchesSearch && !pair.is_active;
    return matchesSearch;
  });

  return (
    <div className="flex flex-col gap-4">
      {/* Header Row */}
      <div className="flex items-center justify-between animate-fade-up">
        <div>
          <h2 className="text-[22px] font-bold text-white tracking-[-0.025em]">Kanallar Markazi</h2>
          <p className="text-[12px] text-white/55">Jami ulangan juftliklar: {pairs.length} ta</p>
        </div>

        <button
          onClick={() => {
            telegram.impact('medium');
            onOpenAddModal();
          }}
          className="vision-btn-blue py-2.5 px-4 flex items-center gap-1.5 text-[12px] font-semibold tracking-tight active:scale-95 transition-transform"
        >
          <Plus size={15} strokeWidth={2.5} />
          <span>Kanal Ulash</span>
        </button>
      </div>

      {/* visionOS Search Capsule (From Image 2 & 4) */}
      <div className="relative w-full animate-fade-up stagger-1">
        <div className="vision-search-pill px-3.5 py-2.5 flex items-center gap-2.5">
          <Search size={15} className="text-white/45 shrink-0" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Kanal nomi yoki @username bo'yicha qidirish..."
            className="w-full bg-transparent text-[13px] text-white placeholder-white/40 focus:outline-none"
          />
          {searchQuery ? (
            <button
              onClick={() => {
                telegram.selection();
                setSearchQuery('');
              }}
              className="text-white/40 hover:text-white shrink-0 p-0.5"
            >
              <X size={14} />
            </button>
          ) : (
            <Mic size={15} className="text-white/30 shrink-0 pointer-events-none" />
          )}
        </div>
      </div>

      {/* visionOS Segmented Control Container (From Images 1, 2, 4) */}
      <div className="vision-segmented-container animate-fade-up stagger-2">
        {[
          { id: 'all', label: `Barchasi (${pairs.length})` },
          { id: 'active', label: `Faol (${pairs.filter((p) => p.is_active).length})` },
          { id: 'paused', label: `To'xtatilgan (${pairs.filter((p) => !p.is_active).length})` },
        ].map((f) => (
          <button
            key={f.id}
            onClick={() => {
              telegram.selection();
              setStatusFilter(f.id as any);
            }}
            className={`vision-segment-pill ${statusFilter === f.id ? 'active' : ''}`}
          >
            {f.label}
          </button>
        ))}
      </div>

      {/* Channel Pairs List or Empty State */}
      {filteredPairs.length === 0 ? (
        <div className="vision-glass-panel p-8 text-center flex flex-col items-center justify-center gap-3">
          <div className="apple-squircle-badge bg-[#0A84FF]/20 border border-[#0A84FF]/30 w-12 h-12 rounded-[14px] flex items-center justify-center">
            <Radio size={22} className="text-[#64D2FF]" />
          </div>
          <div>
            <h4 className="text-[15px] font-semibold text-white">
              {searchQuery ? "Qidiruv bo'yicha kanal topilmadi" : "Hech qanday kanal topilmadi"}
            </h4>
            <p className="text-[12px] text-white/55 mt-1 max-w-xs leading-relaxed">
              {searchQuery
                ? `"${searchQuery}" so'ziga mos keladigan kanal juftligi mavjud emas.`
                : "Yangi kanal bog'lash va avto-ko'chirishni yoqish uchun yuqoridagi 'Kanal Ulash' tugmasini bosing."}
            </p>
          </div>
          {searchQuery ? (
            <button
              onClick={() => setSearchQuery('')}
              className="mt-1 vision-btn-glass py-2 px-4 text-[12px] font-semibold text-white/80 active:scale-95"
            >
              Qidiruvni tozalash
            </button>
          ) : (
            <button
              onClick={onOpenAddModal}
              className="mt-1 vision-btn-blue py-2.5 px-5 text-[12px] font-semibold active:scale-95 transition-transform"
            >
              Hozir Bog'lash
            </button>
          )}
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          {filteredPairs.map((pair) => (
            <div
              key={pair.id}
              className={`vision-glass-panel p-4 flex flex-col gap-3 relative overflow-hidden transition-all duration-200 hover:translate-y-[-1px] ${
                pair.is_active ? '' : 'opacity-65'
              }`}
            >
              {/* Channel Routing Info with Colored Squircles (From Image 4) */}
              <div className="flex items-center justify-between gap-3">
                <div
                  onClick={() => {
                    telegram.selection();
                    onSelectPair(pair);
                  }}
                  className="flex-1 min-w-0 cursor-pointer flex items-center gap-3"
                >
                  {/* Source Icon: Blue Squircle */}
                  <div className="apple-squircle-badge bg-[#0A84FF] w-9 h-9">
                    <Radio size={16} />
                  </div>

                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5 text-[14px] font-semibold text-white">
                      <span className="truncate max-w-[120px]">{pair.source_title || pair.source_channel}</span>
                      <ArrowRight size={13} className="text-[#0A84FF] shrink-0" />
                      <span className="truncate max-w-[120px] text-[#64D2FF]">
                        {pair.target_title || pair.target_channel}
                      </span>
                    </div>

                    <div className="flex items-center gap-1.5 mt-0.5 text-[11px] text-white/50 font-mono">
                      <span 
                        onClick={(e) => handleCopyChannel(pair.source_channel, e)}
                        className="truncate hover:text-white underline decoration-white/20 cursor-copy"
                        title="Nusxalash"
                      >
                        {pair.source_channel}
                      </span>
                      <span>&rarr;</span>
                      <span 
                        onClick={(e) => handleCopyChannel(pair.target_channel, e)}
                        className="truncate hover:text-white underline decoration-white/20 cursor-copy"
                        title="Nusxalash"
                      >
                        {pair.target_channel}
                      </span>
                      {copiedChannel && (copiedChannel === pair.source_channel || copiedChannel === pair.target_channel) && (
                        <span className="text-[10px] text-[#30D158] flex items-center gap-0.5">
                          <Check size={10} /> Nusxalandi
                        </span>
                      )}
                    </div>
                  </div>
                </div>

                {/* Cupertino Switch from Image 4 */}
                <div className="shrink-0 flex items-center">
                  <Switch
                    checked={pair.is_active}
                    onChange={() => onTogglePair(pair.id)}
                  />
                </div>
              </div>

              {/* Badges Strip with Apple HIG Icons */}
              <div
                onClick={() => {
                  telegram.selection();
                  onSelectPair(pair);
                }}
                className="flex items-center gap-1.5 flex-wrap pt-2.5 border-t border-white/[0.08] cursor-pointer"
              >
                <span className="vision-btn-glass px-2.5 py-0.5 text-[10px] font-medium text-white/80">
                  {pair.clone_mode === 'clean' ? 'Toza Nusxa' : 'Forward'}
                </span>

                {pair.image_watermark_type !== 'none' && (
                  <span className="vision-btn-glass px-2.5 py-0.5 text-[10px] font-medium text-[#FFD60A] border-[#FFD60A]/30 bg-[#FFD60A]/10 flex items-center gap-1">
                    <ImageIcon size={10} />
                    <span>Watermark: {pair.image_watermark_text || 'Faol'}</span>
                  </span>
                )}

                {pair.auto_translate && (
                  <span className="vision-btn-glass px-2.5 py-0.5 text-[10px] font-medium text-[#64D2FF] border-[#0A84FF]/30 bg-[#0A84FF]/10 flex items-center gap-1">
                    <Globe size={10} />
                    <span>{pair.target_lang?.toUpperCase()} Tarjima</span>
                  </span>
                )}

                {pair.clean_links && (
                  <span className="vision-btn-glass px-2.5 py-0.5 text-[10px] font-medium text-[#30D158] border-emerald-500/30 bg-emerald-500/10 flex items-center gap-1">
                    <ShieldCheck size={10} />
                    <span>Reklama Toza</span>
                  </span>
                )}

                <div className="ml-auto flex items-center gap-1 text-[11px] text-[#64D2FF] font-medium">
                  <Sliders size={12} />
                  <span>Sozlash</span>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};
