import React, { useState } from 'react';
import {
  X,
  Save,
  Trash2,
  Send,
  Sparkles,
  Layers,
  Sliders,
  Type,
  Shield,
  Plus,
  AlertTriangle,
  Lock
} from 'lucide-react';
import { ChannelPair, Subscription, WatermarkType } from '../../types';
import { Switch } from '../ui/Switch';
import { PostPreviewSimulator } from './PostPreviewSimulator';
import { telegram } from '../../services/telegram';
import { ErrorExplanation, explainError } from '../../services/api';

interface ChannelEditorModalProps {
  pair: ChannelPair;
  subscription: Subscription | null;
  isAdmin: boolean;
  onClose: () => void;
  onSave: (pairId: number, changes: Partial<ChannelPair>) => Promise<void>;
  onDelete: (id: number) => Promise<void>;
  onTestPost: (id: number) => Promise<void>;
  onOpenBilling: () => void;
}

// Limits enforced by the API (config/limits.py)
const SIGNATURE_MAX_CHARS = 1024;
const BLACKLIST_MAX_CHARS = 4000;
const WATERMARK_TEXT_MAX_CHARS = 64;

// Fields this editor can change; only the ones that differ from the stored pair are sent
const EDITABLE_FIELDS: (keyof ChannelPair)[] = [
  'is_active', 'clone_mode', 'clean_links', 'custom_signature', 'auto_translate', 'target_lang',
  'blacklist_words', 'image_watermark_type', 'image_watermark_text', 'image_watermark_pos',
  'ai_paraphrase_mode', 'drip_delay_minutes'
];

const UPGRADE_CODES = new Set(['PRO_REQUIRED', 'VIP_REQUIRED', 'PLAN_LIMIT', 'SUBSCRIPTION_INACTIVE']);

function diffPair(original: ChannelPair, edited: ChannelPair): Partial<ChannelPair> {
  const changes: Record<string, unknown> = {};
  for (const key of EDITABLE_FIELDS) {
    if (original[key] !== edited[key]) {
      changes[key] = edited[key];
    }
  }
  return changes as Partial<ChannelPair>;
}

const ProBadge: React.FC = () => (
  <span className="px-2 py-0.5 rounded-full bg-[#0A84FF]/20 text-[#64D2FF] text-[10px] font-bold uppercase flex items-center gap-1">
    <Lock size={9} /> PRO
  </span>
);

export const ChannelEditorModal: React.FC<ChannelEditorModalProps> = ({
  pair,
  subscription,
  isAdmin,
  onClose,
  onSave,
  onDelete,
  onTestPost,
  onOpenBilling,
}) => {
  const [formData, setFormData] = useState<ChannelPair>({ ...pair });
  const [activeSubTab, setActiveSubTab] = useState<'general' | 'watermark' | 'text' | 'ai'>('general');
  const [newBlacklistWord, setNewBlacklistWord] = useState('');
  const [isSaving, setIsSaving] = useState(false);
  const [isTesting, setIsTesting] = useState(false);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [notice, setNotice] = useState<ErrorExplanation | null>(null);

  // Paid features are gated on the server as well; this only avoids pointless round trips
  const hasPro = isAdmin || Boolean(
    subscription?.is_active && (subscription.tier === 'pro' || subscription.tier === 'vip')
  );

  const requirePro = (feature: string) => {
    telegram.notification('warning');
    setNotice({
      message: `${feature} faqat PRO va VIP tariflarida mavjud.`,
      hint: "«Tariflar» bo'limidan tarifni oshiring.",
      code: 'PRO_REQUIRED'
    });
  };

  const handleToggle = (key: 'is_active' | 'clean_links' | 'auto_translate') => {
    setFormData((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  const handleChange = <K extends keyof ChannelPair>(key: K, value: ChannelPair[K]) => {
    setFormData((prev) => ({ ...prev, [key]: value }));
  };

  const blacklistWords = (formData.blacklist_words || '')
    .split(',')
    .map((w) => w.trim())
    .filter(Boolean);

  const handleAddBlacklist = () => {
    const word = newBlacklistWord.trim();
    if (!word) return;
    telegram.impact('light');
    if (!blacklistWords.includes(word)) {
      const joined = [...blacklistWords, word].join(', ');
      if (joined.length > BLACKLIST_MAX_CHARS) {
        setNotice({ message: `Taqiqlangan so'zlar ro'yxati ${BLACKLIST_MAX_CHARS} belgidan oshmasligi kerak.` });
        return;
      }
      handleChange('blacklist_words', joined);
    }
    setNewBlacklistWord('');
  };

  const handleRemoveBlacklist = (wordToRemove: string) => {
    telegram.impact('light');
    handleChange('blacklist_words', blacklistWords.filter((w) => w !== wordToRemove).join(', '));
  };

  const setWatermarkType = (type: WatermarkType) => {
    telegram.selection();
    if (type !== 'none' && type !== pair.image_watermark_type && !hasPro) {
      requirePro('Rasmga suv belgisi (Watermark)');
      return;
    }
    handleChange('image_watermark_type', type);
  };

  const handleSave = async () => {
    const changes = diffPair(pair, formData);
    setIsSaving(true);
    setNotice(null);
    telegram.impact('medium');
    try {
      await onSave(pair.id, changes);
      onClose();
    } catch (err) {
      setNotice(explainError(err, 'Saqlashda xatolik'));
    } finally {
      setIsSaving(false);
    }
  };

  const handleTest = async () => {
    setIsTesting(true);
    telegram.impact('light');
    try {
      await onTestPost(pair.id);
    } finally {
      setIsTesting(false);
    }
  };

  const handleDeleteConfirmed = async () => {
    telegram.impact('heavy');
    await onDelete(pair.id);
    onClose();
  };

  const positions = [
    { id: 'top_left', label: 'Yuqori Chap' },
    { id: 'top_center', label: "Yuqori O'rta" },
    { id: 'top_right', label: "Yuqori O'ng" },
    { id: 'center_left', label: "O'rta Chap" },
    { id: 'center', label: 'Markaz' },
    { id: 'center_right', label: "O'rta O'ng" },
    { id: 'bottom_left', label: 'Pastki Chap' },
    { id: 'bottom_center', label: "Pastki O'rta" },
    { id: 'bottom_right', label: "Pastki O'ng" },
  ];

  // A logo watermark needs an image on the server, which the Mini App cannot upload: the option is only
  // shown for pairs that already use one.
  const watermarkTypes: WatermarkType[] = pair.image_watermark_type === 'logo' ? ['none', 'text', 'logo'] : ['none', 'text'];
  const watermarkEditable = hasPro || formData.image_watermark_type === 'none';

  return (
    <div className="fixed inset-0 z-50 flex flex-col justify-end bg-black/60 backdrop-blur-2xl animate-fade-in">
      <div
        className="w-full max-h-[92vh] rounded-t-[32px] bg-[#1A191F]/90 backdrop-blur-3xl border-t border-white/25 flex flex-col overflow-hidden shadow-2xl animate-slide-up"
        style={{ paddingBottom: 'max(env(safe-area-inset-bottom, 0px), 12px)', borderTopColor: 'rgba(255,255,255,0.40)' }}
      >
        {/* Apple Sheet Pull Handle */}
        <div className="w-10 h-1.25 rounded-full bg-white/30 mx-auto mt-3 mb-1 shrink-0" />

        {/* Modal Header */}
        <div className="px-5 py-2.5 flex items-center justify-between shrink-0">
          <div className="flex items-center gap-2.5">
            <div className="apple-squircle-badge bg-[#0A84FF] w-8 h-8 rounded-[9px]">
              <Sliders size={15} />
            </div>
            <div>
              <h3 className="text-[15px] font-bold text-white leading-tight">
                {formData.target_title || formData.target_channel}
              </h3>
              <p className="text-[11px] text-white/50 font-mono truncate max-w-[200px]">
                {formData.source_channel} &rarr; {formData.target_channel}
              </p>
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

        {/* Cupertino Segmented Subtabs */}
        <div className="px-4 py-2 shrink-0">
          <div className="vision-segmented-container">
            {([
              { id: 'general', label: 'Asosiy', icon: Layers },
              { id: 'watermark', label: 'Suv Belgisi', icon: Sparkles },
              { id: 'text', label: 'Matn', icon: Type },
              { id: 'ai', label: 'AI & Drip', icon: Shield },
            ] as const).map((tab) => {
              const Icon = tab.icon;
              const isActive = activeSubTab === tab.id;
              return (
                <button
                  key={tab.id}
                  onClick={() => {
                    telegram.selection();
                    setActiveSubTab(tab.id);
                  }}
                  className={`vision-segment-pill gap-1.5 ${isActive ? 'active' : ''}`}
                >
                  <Icon size={12} />
                  <span>{tab.label}</span>
                </button>
              );
            })}
          </div>
        </div>

        {/* Form Body */}
        <div className="flex-1 min-h-0 overflow-y-auto p-4 flex flex-col gap-3.5">

          {/* Post Preview Simulator */}
          <PostPreviewSimulator
            sourceTitle={formData.source_title}
            targetTitle={formData.target_title}
            cloneMode={formData.clone_mode}
            signature={formData.custom_signature}
            watermarkType={formData.image_watermark_type}
            watermarkText={formData.image_watermark_text}
            watermarkPos={formData.image_watermark_pos}
            targetLang={formData.target_lang}
            autoTranslate={formData.auto_translate}
          />

          {/* TAB 1: GENERAL */}
          {activeSubTab === 'general' && (
            <div className="vision-grouped-list shrink-0 p-4 flex flex-col gap-3.5">
              {/* Active Toggle */}
              <div className="flex items-center justify-between">
                <div>
                  <div className="text-[13px] font-semibold text-white">Avtomatik Klonlash</div>
                  <div className="text-[11px] text-white/50">Post chiqqanda darhol ko'chirish</div>
                </div>
                <Switch
                  checked={formData.is_active}
                  onChange={() => handleToggle('is_active')}
                />
              </div>

              <div className="vision-hairline ml-0" />

              {/* Mode Selection */}
              <div className="flex flex-col gap-2">
                <div className="text-[13px] font-semibold text-white">Klonlash Usuli</div>
                <div className="grid grid-cols-2 gap-2">
                  <button
                    type="button"
                    onClick={() => {
                      telegram.selection();
                      handleChange('clone_mode', 'clean');
                    }}
                    className={`py-2 px-3 rounded-[16px] text-xs font-semibold border transition-all ${
                      formData.clone_mode === 'clean'
                        ? 'bg-white/[0.18] border-[#0A84FF] text-[#64D2FF] shadow-sm'
                        : 'bg-white/[0.04] border-white/10 text-white/60'
                    }`}
                  >
                    Toza Nusxa (Clean)
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      telegram.selection();
                      handleChange('clone_mode', 'forward');
                    }}
                    className={`py-2 px-3 rounded-[16px] text-xs font-semibold border transition-all ${
                      formData.clone_mode === 'forward'
                        ? 'bg-white/[0.18] border-[#0A84FF] text-[#64D2FF] shadow-sm'
                        : 'bg-white/[0.04] border-white/10 text-white/60'
                    }`}
                  >
                    Oddiy Forward
                  </button>
                </div>
              </div>

              <div className="vision-hairline ml-0" />

              {/* Clean Links */}
              <div className="flex items-center justify-between">
                <div>
                  <div className="text-[13px] font-semibold text-white">Havolalarni Tozalash</div>
                  <div className="text-[11px] text-white/50">Begona linklarni o'chirish</div>
                </div>
                <Switch
                  checked={formData.clean_links}
                  onChange={() => handleToggle('clean_links')}
                />
              </div>
            </div>
          )}

          {/* TAB 2: WATERMARK */}
          {activeSubTab === 'watermark' && (
            <div className="vision-grouped-list shrink-0 p-4 flex flex-col gap-3.5">
              <div className="flex flex-col gap-2">
                <div className="flex items-center justify-between">
                  <span className="text-[13px] font-semibold text-white">Rasmga Suv Belgisi</span>
                  {!hasPro && <ProBadge />}
                </div>
                <div className={`grid gap-2 ${watermarkTypes.length === 3 ? 'grid-cols-3' : 'grid-cols-2'}`}>
                  {watermarkTypes.map((type) => (
                    <button
                      key={type}
                      type="button"
                      onClick={() => setWatermarkType(type)}
                      className={`py-2 rounded-[14px] text-xs font-semibold transition-all border ${
                        formData.image_watermark_type === type
                          ? 'bg-white/[0.18] border-[#0A84FF] text-[#64D2FF]'
                          : 'bg-white/[0.04] border-white/10 text-white/60'
                      }`}
                    >
                      {type === 'none' ? "Yo'q" : type === 'text' ? 'Matn' : 'Logo'}
                    </button>
                  ))}
                </div>
              </div>

              {formData.image_watermark_type === 'text' && (
                <div className="flex flex-col gap-1.5 pt-2 border-t border-white/10">
                  <label className="text-xs font-semibold text-white">Suv Belgisi Matni</label>
                  <input
                    type="text"
                    value={formData.image_watermark_text}
                    maxLength={WATERMARK_TEXT_MAX_CHARS}
                    disabled={!watermarkEditable}
                    onChange={(e) => handleChange('image_watermark_text', e.target.value)}
                    placeholder="@sizning_kanalingiz"
                    className="vision-input disabled:opacity-50"
                  />
                </div>
              )}

              {formData.image_watermark_type !== 'none' && (
                <div className="flex flex-col gap-2 pt-2 border-t border-white/10">
                  <div className="text-xs font-semibold text-white">Joylashuv (9-To'r)</div>
                  <div className="grid grid-cols-3 gap-1.5">
                    {positions.map((pos) => (
                      <button
                        key={pos.id}
                        type="button"
                        onClick={() => {
                          telegram.impact('light');
                          if (!watermarkEditable) {
                            requirePro('Rasmga suv belgisi (Watermark)');
                            return;
                          }
                          handleChange('image_watermark_pos', pos.id);
                        }}
                        className={`py-2 px-1 text-[11px] font-medium rounded-[12px] border text-center transition-all ${
                          formData.image_watermark_pos === pos.id
                            ? 'bg-[#0A84FF] border-[#0A84FF] text-white font-semibold'
                            : 'bg-white/[0.04] border-white/10 text-white/50'
                        }`}
                      >
                        {pos.label}
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}

          {/* TAB 3: TEXT */}
          {activeSubTab === 'text' && (
            <div className="vision-grouped-list shrink-0 p-4 flex flex-col gap-3.5">
              <div className="flex flex-col gap-1.5">
                <label className="text-[13px] font-semibold text-white">Shaxsiy Imzo (Signature)</label>
                <textarea
                  rows={2}
                  value={formData.custom_signature}
                  maxLength={SIGNATURE_MAX_CHARS}
                  onChange={(e) => handleChange('custom_signature', e.target.value)}
                  placeholder="👉 @mening_kanalim ga obuna bo'ling!"
                  className="vision-input resize-none"
                />
              </div>

              <div className="vision-hairline ml-0" />

              <div className="flex items-center justify-between">
                <div>
                  <div className="text-[13px] font-semibold text-white">Avto-Tarjima (Auto-Translate)</div>
                  <div className="text-[11px] text-white/50">Begona tillarni o'zbekchaga o'girish</div>
                </div>
                <Switch
                  checked={formData.auto_translate}
                  onChange={() => handleToggle('auto_translate')}
                />
              </div>

              {formData.auto_translate && (
                <div className="grid grid-cols-4 gap-2 pt-2 border-t border-white/10">
                  {[
                    { code: 'uz', label: '🇺🇿 Uz' },
                    { code: 'ru', label: '🇷🇺 Ru' },
                    { code: 'en', label: '🇬🇧 En' },
                    { code: 'tr', label: '🇹🇷 Tr' },
                  ].map((lang) => (
                    <button
                      key={lang.code}
                      type="button"
                      onClick={() => {
                        telegram.selection();
                        handleChange('target_lang', lang.code);
                      }}
                      className={`py-1.5 rounded-[12px] text-[11px] font-medium transition-all border ${
                        formData.target_lang === lang.code
                          ? 'bg-[#0A84FF] border-[#0A84FF] text-white font-bold shadow-sm'
                          : 'bg-white/[0.04] border-white/10 text-white/60'
                      }`}
                    >
                      {lang.label}
                    </button>
                  ))}
                </div>
              )}

              <div className="vision-hairline ml-0" />

              {/* Blacklist Words */}
              <div className="flex flex-col gap-2">
                <div className="text-[13px] font-semibold text-white">Taqiqlangan So'zlar (Filtr)</div>
                <div className="flex gap-2">
                  <input
                    type="text"
                    value={newBlacklistWord}
                    maxLength={100}
                    onChange={(e) => setNewBlacklistWord(e.target.value)}
                    placeholder="So'z qo'shish..."
                    className="vision-input flex-1"
                  />
                  <button
                    type="button"
                    onClick={handleAddBlacklist}
                    className="vision-btn-blue px-3 py-1.5 text-xs font-semibold shrink-0"
                  >
                    <Plus size={14} />
                  </button>
                </div>
                <div className="flex flex-wrap gap-1.5 mt-1">
                  {blacklistWords.map((word) => (
                    <span
                      key={word}
                      className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full vision-btn-glass text-[11px] text-white/80"
                    >
                      {word}
                      <button
                        type="button"
                        onClick={() => handleRemoveBlacklist(word)}
                        className="hover:text-rose-400 p-0.5"
                      >
                        <X size={11} />
                      </button>
                    </span>
                  ))}
                </div>
              </div>
            </div>
          )}

          {/* TAB 4: AI & DRIP */}
          {activeSubTab === 'ai' && (
            <div className="vision-grouped-list shrink-0 p-4 flex flex-col gap-3.5">
              <div className="flex flex-col gap-2">
                <div className="flex items-center justify-between">
                  <span className="text-[13px] font-semibold text-white">AI Content Paraphraser</span>
                  {!hasPro && <ProBadge />}
                </div>
                <div className="grid grid-cols-4 gap-1.5">
                  {[
                    { id: 'off', label: 'Off' },
                    { id: 'short', label: 'Tezis' },
                    { id: 'hype', label: 'Hype' },
                    { id: 'formal', label: 'Rasmiy' },
                  ].map((mode) => (
                    <button
                      key={mode.id}
                      type="button"
                      onClick={() => {
                        telegram.selection();
                        if (mode.id !== 'off' && mode.id !== pair.ai_paraphrase_mode && !hasPro) {
                          requirePro('AI Content Paraphraser');
                          return;
                        }
                        handleChange('ai_paraphrase_mode', mode.id);
                      }}
                      className={`py-1.5 rounded-[12px] text-xs font-medium transition-all border ${
                        formData.ai_paraphrase_mode === mode.id
                          ? 'bg-[#0A84FF] border-[#0A84FF] text-white font-bold shadow-sm'
                          : 'bg-white/[0.04] border-white/10 text-white/60'
                      }`}
                    >
                      {mode.label}
                    </button>
                  ))}
                </div>
              </div>

              <div className="vision-hairline ml-0" />

              {/* Drip Feed */}
              <div className="flex flex-col gap-2">
                <div className="flex items-center justify-between">
                  <span className="text-[13px] font-semibold text-white flex items-center gap-2">
                    Drip Feed Kechikishi {!hasPro && <ProBadge />}
                  </span>
                  <span className="text-xs text-[#0A84FF] font-mono">
                    {formData.drip_delay_minutes === 0 ? 'Darhol' : `${formData.drip_delay_minutes} daqiqa`}
                  </span>
                </div>
                <div className="grid grid-cols-5 gap-1.5">
                  {[0, 5, 15, 30, 45].map((mins) => (
                    <button
                      key={mins}
                      type="button"
                      onClick={() => {
                        telegram.impact('light');
                        if (mins > 0 && mins !== pair.drip_delay_minutes && !hasPro) {
                          requirePro('Drip Feed kechikishi');
                          return;
                        }
                        handleChange('drip_delay_minutes', mins);
                      }}
                      className={`py-1.5 rounded-[12px] text-xs font-medium transition-all border ${
                        formData.drip_delay_minutes === mins
                          ? 'bg-[#0A84FF] border-[#0A84FF] text-white font-bold shadow-sm'
                          : 'bg-white/[0.04] border-white/10 text-white/60'
                      }`}
                    >
                      {mins === 0 ? '0m' : `${mins}m`}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          )}
        </div>

        {/* Validation / plan notices from the client checks or the server */}
        {notice && (
          <div
            role="alert"
            className="mx-4 mb-2 p-3 rounded-[18px] bg-rose-950/70 border border-rose-500/40 text-rose-200 text-xs flex items-start gap-2 shrink-0"
          >
            <AlertTriangle size={14} className="text-rose-400 shrink-0 mt-0.5" />
            <div className="flex-1 flex flex-col gap-1">
              <span className="leading-relaxed">{notice.message}</span>
              {notice.hint && <span className="text-rose-100/70 leading-relaxed">{notice.hint}</span>}
              {notice.code && UPGRADE_CODES.has(notice.code) && (
                <button
                  type="button"
                  onClick={onOpenBilling}
                  className="self-start mt-1 py-1 px-3 rounded-full vision-btn-gold text-[11px] font-bold"
                >
                  Tariflar
                </button>
              )}
            </div>
            <button type="button" onClick={() => setNotice(null)} className="text-white/40 hover:text-white p-0.5 shrink-0">
              <X size={12} />
            </button>
          </div>
        )}

        {/* In-App Delete Confirmation Bar (Replaces raw browser confirm!) */}
        {showDeleteConfirm ? (
          <div className="p-4 border-t border-rose-500/30 bg-rose-950/80 backdrop-blur-md flex items-center justify-between gap-3 shrink-0 animate-in fade-in">
            <div className="flex items-center gap-2 text-rose-200 text-xs">
              <AlertTriangle size={16} className="text-rose-400 shrink-0" />
              <span>Kanalni o'chirishni tasdiqlaysizmi?</span>
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => setShowDeleteConfirm(false)}
                className="py-1.5 px-3 rounded-full vision-btn-glass text-xs font-medium text-white/70"
              >
                Yo'q
              </button>
              <button
                type="button"
                onClick={handleDeleteConfirmed}
                className="py-1.5 px-3 rounded-full bg-rose-600 hover:bg-rose-500 text-white text-xs font-bold active:scale-95 transition-transform"
              >
                Ha, O'chirish
              </button>
            </div>
          </div>
        ) : (
          /* Modal Action Buttons Footer */
          <div className="p-4 border-t border-white/10 bg-[#121317]/90 backdrop-blur-md flex items-center justify-between gap-2.5 shrink-0">
            <button
              type="button"
              onClick={() => {
                telegram.impact('medium');
                setShowDeleteConfirm(true);
              }}
              className="w-11 h-11 rounded-full vision-btn-glass text-rose-400 hover:text-rose-300 flex items-center justify-center active:scale-95 transition-transform shrink-0"
              title="Kanalni o'chirish"
            >
              <Trash2 size={16} />
            </button>

            <button
              type="button"
              onClick={handleTest}
              disabled={isTesting}
              className="flex-1 py-3 px-3 rounded-full vision-btn-glass text-white text-xs font-semibold flex items-center justify-center gap-1.5 active:scale-95 transition-transform disabled:opacity-50"
            >
              <Send size={14} className="text-[#30D158]" />
              <span>{isTesting ? 'Yuborilmoqda...' : 'Sinov Xabari'}</span>
            </button>

            <button
              type="button"
              onClick={handleSave}
              disabled={isSaving}
              className="flex-1 py-3 px-4 rounded-full vision-btn-blue text-white text-xs font-bold flex items-center justify-center gap-1.5 active:scale-95 transition-transform disabled:opacity-50"
            >
              <Save size={14} />
              <span>{isSaving ? 'Saqlanmoqda...' : 'Saqlash'}</span>
            </button>
          </div>
        )}

      </div>
    </div>
  );
};
