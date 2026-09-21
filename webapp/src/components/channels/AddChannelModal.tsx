import React, { useState } from 'react';
import { X, ArrowRight, ArrowLeft, CheckCircle2, ShieldCheck, Radio, Plus } from 'lucide-react';
import { ChannelPair } from '../../types';
import { Switch } from '../ui/Switch';
import { telegram } from '../../services/telegram';

interface AddChannelModalProps {
  onClose: () => void;
  onAdd: (data: Partial<ChannelPair>) => Promise<void>;
}

export const AddChannelModal: React.FC<AddChannelModalProps> = ({ onClose, onAdd }) => {
  const [step, setStep] = useState<1 | 2 | 3>(1);
  const [sourceChannel, setSourceChannel] = useState('');
  const [sourceTitle, setSourceTitle] = useState('');
  const [targetChannel, setTargetChannel] = useState('');
  const [targetTitle, setTargetTitle] = useState('');
  const [cloneMode, setCloneMode] = useState<'clean' | 'forward'>('clean');
  const [cleanLinks, setCleanLinks] = useState(true);
  const [autoTranslate, setAutoTranslate] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState('');

  const handleNextStep1 = () => {
    if (!sourceChannel.trim()) {
      setError('Iltimos, manba kanalini kiriting');
      telegram.notification('error');
      return;
    }
    setError('');
    telegram.impact('light');
    setStep(2);
  };

  const handleNextStep2 = () => {
    if (!targetChannel.trim()) {
      setError('Iltimos, o\'zingizning kanalingizni kiriting');
      telegram.notification('error');
      return;
    }
    setError('');
    telegram.impact('light');
    setStep(3);
  };

  const handleSubmit = async () => {
    setIsSubmitting(true);
    telegram.impact('medium');
    try {
      await onAdd({
        source_channel: sourceChannel.trim(),
        source_title: sourceTitle.trim() || sourceChannel.trim(),
        target_channel: targetChannel.trim(),
        target_title: targetTitle.trim() || targetChannel.trim(),
        clone_mode: cloneMode,
        clean_links: cleanLinks,
        auto_translate: autoTranslate,
        is_active: true
      });
      onClose();
    } catch (err: any) {
      setError(err.message || 'Xatolik yuz berdi');
      telegram.notification('error');
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex flex-col justify-end bg-black/60 backdrop-blur-2xl animate-fade-in">
      <div 
        className="w-full max-h-[90vh] rounded-t-[32px] bg-[#1A191F]/90 backdrop-blur-3xl border-t border-white/25 flex flex-col overflow-hidden shadow-2xl animate-slide-up"
        style={{ paddingBottom: 'max(env(safe-area-inset-bottom, 0px), 12px)', borderTopColor: 'rgba(255,255,255,0.40)' }}
      >
        {/* Apple Sheet Pull Handle */}
        <div className="w-10 h-1.25 rounded-full bg-white/30 mx-auto mt-3 mb-1 shrink-0" />

        {/* Header */}
        <div className="px-5 py-2.5 flex items-center justify-between shrink-0">
          <div className="flex items-center gap-2.5">
            <div className="apple-squircle-badge bg-[#0A84FF] w-7 h-7 rounded-[8px] text-white text-xs font-bold">
              {step}
            </div>
            <div>
              <h3 className="text-[16px] font-bold text-white tracking-tight">Yangi Kanal Ulash</h3>
              <p className="text-[11px] text-white/50">{step}/3-bosqich</p>
            </div>
          </div>
          <button
            type="button"
            onClick={() => {
              telegram.selection();
              onClose();
            }}
            className="w-8 h-8 rounded-full vision-btn-glass text-white/70 hover:text-white flex items-center justify-center active:scale-95 transition-transform"
          >
            <X size={15} />
          </button>
        </div>

        {/* Step Indicator Progress Bar */}
        <div className="w-full bg-white/[0.08] h-1">
          <div
            className="bg-[#0A84FF] h-full transition-all duration-300 shadow-[0_0_8px_rgba(10,132,255,0.8)]"
            style={{ width: `${(step / 3) * 100}%` }}
          />
        </div>

        {/* Modal Body */}
        <div className="flex-1 min-h-0 p-5 flex flex-col gap-4 overflow-y-auto">
          {error && (
            <div className="p-3 rounded-[18px] bg-rose-950/70 border border-rose-500/40 text-rose-200 text-xs flex items-center gap-2 animate-in fade-in">
              <span className="w-1.5 h-1.5 rounded-full bg-rose-400" />
              <span>{error}</span>
            </div>
          )}

          {/* STEP 1: SOURCE CHANNEL */}
          {step === 1 && (
            <div className="vision-grouped-list shrink-0 p-4 flex flex-col gap-3.5">
              <div className="flex items-center gap-2 text-[14px] font-semibold text-white">
                <Radio size={16} className="text-[#0A84FF]" />
                <span>1-Qadam: Manba Kanal (Qayerdan?)</span>
              </div>
              <p className="text-[12px] text-white/60 leading-relaxed">
                Postlarini ko'chirmoqchi bo'lgan ommaviy yoki yopiq kanalingiz username yoki havolasini kiriting.
              </p>

              <div className="flex flex-col gap-1.5 mt-1">
                <label className="text-[11px] font-medium text-white/70">Kanal havolasi yoki @username</label>
                <input
                  type="text"
                  value={sourceChannel}
                  onChange={(e) => setSourceChannel(e.target.value)}
                  placeholder="@yangiliklar_kanali"
                  className="vision-input"
                />
              </div>

              <div className="flex flex-col gap-1.5">
                <label className="text-[11px] font-medium text-white/70">Kanal Nomi (Ixtiyoriy)</label>
                <input
                  type="text"
                  value={sourceTitle}
                  onChange={(e) => setSourceTitle(e.target.value)}
                  placeholder="Masalan: Tezkor Yangiliklar"
                  className="vision-input"
                />
              </div>
            </div>
          )}

          {/* STEP 2: TARGET CHANNEL */}
          {step === 2 && (
            <div className="vision-grouped-list shrink-0 p-4 flex flex-col gap-3.5">
              <div className="flex items-center gap-2 text-[14px] font-semibold text-white">
                <Radio size={16} className="text-[#30D158]" />
                <span>2-Qadam: O'zingizning Kanalingiz (Qayerga?)</span>
              </div>
              
              <div className="p-3 rounded-[16px] bg-[#FFD60A]/10 border border-[#FFD60A]/25 text-[#FFD60A] text-xs flex items-start gap-2.5">
                <ShieldCheck size={16} className="shrink-0 mt-0.5" />
                <span className="text-white/85 leading-relaxed text-[12px]">
                  <b className="text-[#FFD60A]">Muhim:</b> Botingiz kanalingizda <b>administrator</b> bo'lishi va post joylash huquqiga ega bo'lishi kerak!
                </span>
              </div>

              <div className="flex flex-col gap-1.5 mt-1">
                <label className="text-[11px] font-medium text-white/70">Kanalingiz (@username yoki ID)</label>
                <input
                  type="text"
                  value={targetChannel}
                  onChange={(e) => setTargetChannel(e.target.value)}
                  placeholder="@mening_kanalim"
                  className="vision-input"
                />
              </div>

              <div className="flex flex-col gap-1.5">
                <label className="text-[11px] font-medium text-white/70">Kanalingiz Nomi (Ixtiyoriy)</label>
                <input
                  type="text"
                  value={targetTitle}
                  onChange={(e) => setTargetTitle(e.target.value)}
                  placeholder="Masalan: Mening Yangi Kanalim"
                  className="vision-input"
                />
              </div>
            </div>
          )}

          {/* STEP 3: PRESETS */}
          {step === 3 && (
            <div className="vision-grouped-list shrink-0 p-4 flex flex-col gap-3.5">
              <div className="text-[14px] font-semibold text-white">
                3-Qadam: Boshlang'ich Sozlamalar
              </div>

              <div className="grid grid-cols-2 gap-2.5">
                <button
                  type="button"
                  onClick={() => {
                    telegram.selection();
                    setCloneMode('clean');
                  }}
                  className={`p-3 rounded-[18px] border text-left flex flex-col gap-1 transition-all ${
                    cloneMode === 'clean'
                      ? 'bg-white/[0.18] border-[#0A84FF] shadow-sm'
                      : 'bg-white/[0.04] border-white/10 text-white/60'
                  }`}
                >
                  <span className={`text-xs font-bold ${cloneMode === 'clean' ? 'text-[#64D2FF]' : 'text-white'}`}>
                    Toza Nusxa
                  </span>
                  <span className="text-[11px] text-white/60">O'z kanalingiz nomidan</span>
                </button>

                <button
                  type="button"
                  onClick={() => {
                    telegram.selection();
                    setCloneMode('forward');
                  }}
                  className={`p-3 rounded-[18px] border text-left flex flex-col gap-1 transition-all ${
                    cloneMode === 'forward'
                      ? 'bg-white/[0.18] border-[#0A84FF] shadow-sm'
                      : 'bg-white/[0.04] border-white/10 text-white/60'
                  }`}
                >
                  <span className={`text-xs font-bold ${cloneMode === 'forward' ? 'text-[#64D2FF]' : 'text-white'}`}>
                    Forward
                  </span>
                  <span className="text-[11px] text-white/60">Manba kanal ko'rsatiladi</span>
                </button>
              </div>

              {/* Cupertino Switch rows instead of raw checkboxes! */}
              <div className="flex flex-col gap-3 mt-2 pt-3 border-t border-white/[0.08]">
                <div className="flex items-center justify-between">
                  <div>
                    <div className="text-[13px] font-semibold text-white">Havolalarni Tozalash</div>
                    <div className="text-[11px] text-white/50">Begona link va reklamalarni o'chirish</div>
                  </div>
                  <Switch
                    checked={cleanLinks}
                    onChange={(val) => setCleanLinks(val)}
                  />
                </div>

                <div className="vision-hairline ml-0" />

                <div className="flex items-center justify-between">
                  <div>
                    <div className="text-[13px] font-semibold text-white">Avto-Tarjima</div>
                    <div className="text-[11px] text-white/50">O'zbek tiliga avtomatik o'girish</div>
                  </div>
                  <Switch
                    checked={autoTranslate}
                    onChange={(val) => setAutoTranslate(val)}
                  />
                </div>
              </div>
            </div>
          )}
        </div>

        {/* Footer Navigation Buttons */}
        <div className="p-4 border-t border-white/10 bg-[#121317]/90 flex items-center justify-between gap-3 shrink-0">
          {step > 1 ? (
            <button
              type="button"
              onClick={() => {
                telegram.selection();
                setStep((prev) => (prev - 1) as any);
              }}
              className="py-2.5 px-4 rounded-full vision-btn-glass text-xs font-semibold text-white/80 flex items-center gap-1.5 active:scale-95"
            >
              <ArrowLeft size={14} /> Ortga
            </button>
          ) : (
            <button
              type="button"
              onClick={onClose}
              className="py-2.5 px-4 rounded-full vision-btn-glass text-xs font-semibold text-white/60 active:scale-95"
            >
              Bekor qilish
            </button>
          )}

          {step < 3 ? (
            <button
              type="button"
              onClick={step === 1 ? handleNextStep1 : handleNextStep2}
              className="py-2.5 px-5 rounded-full vision-btn-blue text-white text-xs font-bold flex items-center gap-1.5 active:scale-95"
            >
              Keyingisi <ArrowRight size={14} />
            </button>
          ) : (
            <button
              type="button"
              onClick={handleSubmit}
              disabled={isSubmitting}
              className="py-2.5 px-5 rounded-full vision-btn-blue text-white text-xs font-bold flex items-center gap-1.5 active:scale-95 disabled:opacity-50"
            >
              <CheckCircle2 size={14} />
              <span>{isSubmitting ? 'Ulanmoqda...' : 'Ulashni Yakunlash'}</span>
            </button>
          )}
        </div>

      </div>
    </div>
  );
};
