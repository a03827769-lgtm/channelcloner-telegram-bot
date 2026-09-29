import React, { useEffect, useState } from 'react';
import { History, Play, RefreshCw, Layers } from 'lucide-react';
import { ChannelPair } from '../../types';
import { telegram } from '../../services/telegram';
import { errorText } from '../../services/api';

interface BackfillTabProps {
  pairs: ChannelPair[];
  onTriggerBackfill: (pairId: number, count: number) => Promise<void>;
}

export const BackfillTab: React.FC<BackfillTabProps> = ({ pairs, onTriggerBackfill }) => {
  const [selectedPairId, setSelectedPairId] = useState<number>(pairs[0]?.id || 0);
  const [postCount, setPostCount] = useState<number>(20);
  const [isRunning, setIsRunning] = useState<boolean>(false);
  const [logMessages, setLogMessages] = useState<string[]>([]);

  // Keep the selection valid when pairs load later or the selected pair is deleted
  useEffect(() => {
    if (!pairs.some((p) => p.id === selectedPairId)) {
      setSelectedPairId(pairs[0]?.id || 0);
    }
  }, [pairs, selectedPairId]);

  const handleStart = async () => {
    if (!selectedPairId) {
      telegram.notification('error');
      return;
    }
    telegram.impact('medium');
    setIsRunning(true);
    setLogMessages([
      `[Yuborildi] Tarixiy ${postCount} ta xabarni ko'chirish so'rovi yuborilmoqda...`
    ]);

    try {
      await onTriggerBackfill(selectedPairId, postCount);
      setLogMessages((prev) => [
        ...prev,
        `[Navbatga qo'yildi] ${postCount} ta post orqa fonda ko'chiriladi. Yakunlanganda bot sizga xabar yuboradi. 🚀`,
      ]);
      telegram.notification('success');
    } catch (err) {
      setLogMessages((prev) => [
        ...prev,
        `[Xatolik] ${errorText(err, 'Xatolik yuz berdi')}`,
      ]);
      telegram.notification('error');
    } finally {
      setIsRunning(false);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="animate-fade-up">
        <h2 className="text-[22px] font-bold text-white tracking-[-0.025em] flex items-center gap-2">
          <History size={22} className="text-[#64D2FF]" />
          <span>Tarixni Ko'chirish</span>
        </h2>
        <p className="text-[13px] text-white/55 mt-0.5">
          Manba kanaldagi eski postlar, albomlar va videolarni o'zingizning kanalingizga nusxalash.
        </p>
      </div>

      {pairs.length === 0 ? (
        <div className="vision-glass-panel p-8 text-center flex flex-col items-center justify-center gap-3">
          <div className="apple-squircle-badge bg-[#64D2FF]/20 border border-[#64D2FF]/30 w-12 h-12 rounded-[14px] flex items-center justify-center">
            <Layers size={22} className="text-[#64D2FF]" />
          </div>
          <div>
            <h4 className="text-[15px] font-semibold text-white">Ulangan kanallar yo'q</h4>
            <p className="text-[12px] text-white/50 mt-1 max-w-xs leading-relaxed">
              Tarixni ko'chirish uchun avval kamida bitta kanal juftligini ulang.
            </p>
          </div>
        </div>
      ) : (
        <div className="flex flex-col gap-3.5">
          {/* Channel Selector Pod */}
          <div className="vision-glass-panel p-4 flex flex-col gap-2 animate-fade-up stagger-1">
            <label className="text-[13px] font-semibold text-white">Kanal Juftligini Tanlang</label>
            <div className="relative w-full">
              <select
                value={selectedPairId}
                onChange={(e) => {
                  telegram.selection();
                  setSelectedPairId(Number(e.target.value));
                }}
                className="vision-input vision-select cursor-pointer text-[13px] pr-10"
              >
                {pairs.map((p) => (
                  <option key={p.id} value={p.id} className="bg-[#1C1D22] text-white py-1">
                    {p.source_title || p.source_channel} &rarr; {p.target_title || p.target_channel}
                  </option>
                ))}
              </select>
            </div>
          </div>

          {/* Post Count Presets */}
          <div className="vision-glass-panel p-4 flex flex-col gap-2.5 animate-fade-up stagger-2">
            <div className="flex items-center justify-between">
              <label className="text-[13px] font-semibold text-white">Ko'chirish Ko'lami</label>
              <span className="font-mono text-[#0A84FF] text-xs font-bold">{postCount} ta post</span>
            </div>
            <div className="grid grid-cols-4 gap-2">
              {[10, 20, 50, 100].map((cnt) => (
                <button
                  key={cnt}
                  type="button"
                  onClick={() => {
                    telegram.impact('light');
                    setPostCount(cnt);
                  }}
                  className={`py-2 px-2 rounded-full text-[12px] font-medium transition-all ${
                    postCount === cnt
                      ? 'vision-btn-blue font-bold shadow-sm'
                      : 'vision-btn-glass text-white/70 hover:text-white'
                  }`}
                >
                  {cnt} ta
                </button>
              ))}
            </div>
          </div>

          {/* Progress Indicator */}
          {isRunning && (
            <div className="vision-glass-panel p-4 flex flex-col gap-2.5 animate-in fade-in">
              <div className="flex items-center justify-between text-[13px] font-semibold text-white">
                <span className="flex items-center gap-2">
                  <RefreshCw size={14} className="text-[#0A84FF] animate-spin" />
                  Ko'chirilmoqda...
                </span>
                <span className="font-mono text-[#64D2FF] text-xs">MTProto faol</span>
              </div>
              <div className="w-full h-2 rounded-full bg-white/10 overflow-hidden relative">
                <div className="h-full w-full bg-gradient-to-r from-[#0A84FF] to-[#64D2FF] rounded-full animate-pulse shadow-[0_0_12px_rgba(10,132,255,0.7)]" />
              </div>
            </div>
          )}

          {/* Log Console */}
          {logMessages.length > 0 && (
            <div className="vision-glass-panel p-3.5 font-mono text-[11px] text-white/80 flex flex-col gap-1.5 max-h-40 overflow-y-auto rounded-[20px] bg-black/50 border border-white/10 select-text">
              {logMessages.map((msg, i) => (
                <div key={i} className="leading-relaxed">
                  {msg}
                </div>
              ))}
            </div>
          )}

          {/* Start Button */}
          <button
            onClick={handleStart}
            disabled={isRunning}
            className={`w-full py-3.5 rounded-full animate-fade-up stagger-3 animate-scale-in stagger-4 text-[13px] font-semibold tracking-tight flex items-center justify-center gap-2 transition-all active:scale-[0.97] ${
              isRunning
                ? 'opacity-50 cursor-not-allowed bg-white/10 text-white/50'
                : 'vision-btn-blue text-white'
            }`}
          >
            {isRunning ? (
              <>
                <RefreshCw size={16} className="animate-spin" />
                <span>Nusxalash Bajarilmoqda...</span>
              </>
            ) : (
              <>
                <Play size={16} className="fill-white" />
                <span>Tarixni Ko'chirishni Boshlash</span>
              </>
            )}
          </button>
        </div>
      )}
    </div>
  );
};
