import React, { useState, useEffect, useRef } from 'react';
import {
  Crown,
  Play,
  Pause,
  Music,
  Save,
  MapPin,
  Home,
  DollarSign
} from 'lucide-react';
import { StorySettings, AudioTrack, StoryQueueItem, StoryPostedItem, StoryBackground } from '../../types';
import { Switch } from '../ui/Switch';
import { CupertinoSlider } from '../ui/CupertinoSlider';
import { telegram } from '../../services/telegram';

interface StoryStudioTabProps {
  settings: StorySettings | null;
  audioTracks: AudioTrack[];
  queue: StoryQueueItem[];
  posted: StoryPostedItem[];
  isVip: boolean;
  /** Receives only the fields that differ from the saved settings */
  onSaveSettings: (changes: Partial<StorySettings>) => Promise<void>;
  onOpenBilling: () => void;
}

// Same choices as the bot's story design menu (the API accepts only these)
const BACKGROUND_STYLES: { id: StoryBackground; label: string; color: string }[] = [
  { id: 'telegram_green', label: 'Telegram Yashil', color: '#10B981' },
  { id: 'listing_blur', label: "Xiralashgan Rasm", color: '#64748B' },
  { id: 'luxury_dark', label: "To'q Lux", color: '#1E293B' },
  { id: 'emerald', label: 'Zumrad', color: '#047857' },
];

function diffSettings(original: StorySettings | null, edited: Partial<StorySettings>): Partial<StorySettings> {
  const changes: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(edited)) {
    if (!original || original[key as keyof StorySettings] !== value) {
      changes[key] = value;
    }
  }
  return changes as Partial<StorySettings>;
}

export const StoryStudioTab: React.FC<StoryStudioTabProps> = ({
  settings,
  audioTracks,
  queue,
  posted,
  isVip,
  onSaveSettings,
  onOpenBilling,
}) => {
  const [formData, setFormData] = useState<Partial<StorySettings>>(settings || {});
  const [activeTab, setActiveTab] = useState<'preview' | 'settings' | 'queue'>('preview');
  const [currentSlide, setCurrentSlide] = useState(0);
  const [isPlayingAudio, setIsPlayingAudio] = useState(false);
  const [selectedTrack, setSelectedTrack] = useState<string>('01_luxury_corporate.mp3');
  const [isSaving, setIsSaving] = useState(false);

  const audioRef = useRef<HTMLAudioElement | null>(null);

  useEffect(() => {
    if (settings) {
      setFormData(settings);
    }
  }, [settings]);

  const sampleSlides = [
    'https://images.unsplash.com/photo-1600585154340-be6161a56a0c?auto=format&fit=crop&w=800&q=80',
    'https://images.unsplash.com/photo-1600596542815-ffad4c1539a9?auto=format&fit=crop&w=800&q=80',
    'https://images.unsplash.com/photo-1600607687939-ce8a6c25118c?auto=format&fit=crop&w=800&q=80',
  ];

  useEffect(() => {
    const timer = setInterval(() => {
      setCurrentSlide((prev) => (prev + 1) % sampleSlides.length);
    }, 5000);
    return () => clearInterval(timer);
  }, [sampleSlides.length]);

  const toggleAudio = (trackFile: string) => {
    telegram.impact('light');
    if (audioRef.current) {
      if (isPlayingAudio && selectedTrack === trackFile) {
        audioRef.current.pause();
        setIsPlayingAudio(false);
      } else {
        audioRef.current.src = `/api/audio-tracks/${encodeURIComponent(trackFile)}`;
        audioRef.current.play().catch(() => {});
        setSelectedTrack(trackFile);
        setIsPlayingAudio(true);
      }
    }
  };

  const handleSave = async () => {
    setIsSaving(true);
    telegram.impact('medium');
    try {
      await onSaveSettings(diffSettings(settings, formData));
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <audio ref={audioRef} onEnded={() => setIsPlayingAudio(false)} />

      {/* VIP Flagship Chromatic Card */}
      <div className="vision-intelligence-card p-5 relative overflow-hidden animate-fade-up liquid-glass-shimmer">
        <div className="flex items-start justify-between relative z-10 gap-3">
          <div>
            <div className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full bg-[#FFD60A]/20 border border-[#FFD60A]/40 text-[11px] font-bold uppercase tracking-wider text-[#FFD60A]">
              <Crown size={12} className="text-[#FFD60A]" />
              <span>VIP Exclusive</span>
            </div>
            <h2 className="text-[20px] font-bold text-white tracking-[-0.02em] mt-1.5 leading-tight">
              Real Estate Auto-Story Studio
            </h2>
            <p className="text-[12px] text-white/65 mt-1 max-w-xs leading-relaxed">
              $700+ hashamatli uylarni avtomatik aniqlab, 4K Playwright kollaj va Ken Burns video ko'rinishida Telegram Istoriyasiga avto-joylash.
            </p>
          </div>

          {!isVip && (
            <button
              onClick={onOpenBilling}
              className="vision-btn-gold py-2 px-3.5 text-[12px] font-bold active:scale-95 shrink-0"
            >
              VIP Olish
            </button>
          )}
        </div>
      </div>

      {/* visionOS Segmented Control Container */}
      <div className="vision-segmented-container animate-fade-up stagger-1">
        {([
          { id: 'preview', label: '9:16 Simulyator' },
          { id: 'settings', label: 'Sozlamalar' },
          { id: 'queue', label: `Navbat (${queue.length})` },
        ] as const).map((tab) => (
          <button
            key={tab.id}
            onClick={() => {
              telegram.selection();
              setActiveTab(tab.id);
            }}
            className={`vision-segment-pill ${activeTab === tab.id ? 'active' : ''}`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* TAB 1: 9:16 STORY SIMULATOR */}
      {activeTab === 'preview' && (
        <div className="flex flex-col items-center gap-4">
          {/* Mobile Story Frame */}
          <div className="relative w-full max-w-[280px] h-[495px] rounded-[32px] overflow-hidden shadow-2xl border border-white/25 flex flex-col justify-between bg-black select-none">
            {/* Background Animated Ken Burns Image */}
            <div className="absolute inset-0 overflow-hidden">
              <img
                src={sampleSlides[currentSlide]}
                alt="Luxury Real Estate"
                className="w-full h-full object-cover ken-burns"
              />
              <div className="absolute inset-0 bg-gradient-to-t from-black/90 via-black/20 to-black/60 pointer-events-none" />
            </div>

            {/* Top Progress Bar */}
            <div className="relative z-10 pt-3 px-3 flex flex-col gap-2">
              <div className="flex items-center gap-1.5 w-full">
                {sampleSlides.map((_, idx) => (
                  <div key={idx} className="flex-1 h-1 rounded-full bg-white/30 overflow-hidden">
                    <div
                      className={`h-full bg-white transition-all duration-300 ${
                        idx < currentSlide
                          ? 'w-full'
                          : idx === currentSlide
                          ? 'w-2/3 animate-pulse'
                          : 'w-0'
                      }`}
                    />
                  </div>
                ))}
              </div>

              {/* Profile Bar */}
              <div className="flex items-center justify-between mt-1">
                <div className="flex items-center gap-2">
                  <div className="w-8 h-8 rounded-full border border-[#FFD60A] bg-black flex items-center justify-center text-xs font-bold text-[#FFD60A]">
                    CC
                  </div>
                  <div>
                    <div className="text-[12px] font-semibold text-white leading-none">
                      {formData.source_title || 'Toshkent Hashamatli Uylar'}
                    </div>
                    <span className="text-[10px] text-white/60 font-mono">15 daqiqa oldin</span>
                  </div>
                </div>

                <span className="px-2 py-0.5 rounded-full bg-[#FFD60A] text-black text-[9px] font-bold uppercase tracking-wider">
                  4K VIP
                </span>
              </div>
            </div>

            {/* Middle: Badges Float */}
            <div className="relative z-10 px-3 flex flex-col gap-1.5 items-start">
              <span className="px-2.5 py-1 rounded-full bg-black/60 backdrop-blur-md border border-white/20 text-[11px] font-medium text-white flex items-center gap-1">
                <MapPin size={12} className="text-rose-400" />
                Mirobod tumani (Tashkent City)
              </span>

              <div className="px-3 py-1 rounded-xl bg-gradient-to-r from-[#FFD60A] to-amber-500 text-black text-xs font-bold flex items-center gap-1 shadow-sm">
                <DollarSign size={13} className="stroke-[3]" />
                <span className="tabular-nums font-mono font-extrabold">$1,400 / oyiga</span>
              </div>
            </div>

            {/* Bottom: Luxury Card Info with White Pill Button (From Image 2) */}
            <div className="relative z-10 p-3 flex flex-col gap-1.5 bg-black/75 backdrop-blur-xl border-t border-white/10 rounded-b-[32px]">
              <div className="flex items-center justify-between text-xs text-white font-semibold">
                <span className="flex items-center gap-1">
                  <Home size={13} className="text-[#FFD60A]" /> 4 xona • 160 m² • Penthouse
                </span>
                <span className="text-[10px] text-[#30D158] font-mono">98/100 Sifat</span>
              </div>

              <p className="text-[11px] text-white/70 line-clamp-2 leading-relaxed">
                Yevro-remont, zamonaviy mebellar, panorama derazalar va yer osti avtoturargohi.
              </p>

              {/* White Pill Button matching "Watch" button in Image 2 */}
              <div className="w-full py-2.5 rounded-full vision-btn-white text-center text-[12px] font-bold uppercase tracking-wider shadow-md mt-1 cursor-pointer">
                E'lonni Ko'rish
              </div>
            </div>
          </div>

          {/* visionOS Audio Player Capsule (Modeled after Image 3 player!) */}
          <div className="w-full vision-glass-panel p-4 flex flex-col gap-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2.5">
                <div className="apple-squircle-badge bg-[#FF9F0A] w-7 h-7 rounded-[8px]">
                  <Music size={14} className="text-white" />
                </div>
                <div>
                  <div className="text-[13px] font-semibold text-white">Ambient Soundtracks</div>
                  <div className="text-[10px] text-white/50">{audioTracks.length} ta eksklyuziv soundtrack</div>
                </div>
              </div>
              <span className="text-[10px] text-white/40 font-mono">visionOS Player</span>
            </div>

            {/* Audio Track List */}
            <div className="grid grid-cols-1 gap-2 max-h-44 overflow-y-auto">
              {audioTracks.map((track) => {
                const isCurrent = selectedTrack === track.filename;
                return (
                  <div
                    key={track.filename}
                    onClick={() => toggleAudio(track.filename)}
                    className={`p-2.5 rounded-[16px] border flex items-center justify-between cursor-pointer transition-all ${
                      isCurrent
                        ? 'bg-[#FFD60A]/15 border-[#FFD60A]/40 text-white'
                        : 'bg-white/[0.04] border-white/5 text-white/70 hover:bg-white/[0.08]'
                    }`}
                  >
                    <div className="flex items-center gap-2.5 min-w-0">
                      <div className="w-7 h-7 rounded-full bg-white/10 flex items-center justify-center text-[#FFD60A] shrink-0">
                        {isPlayingAudio && isCurrent ? (
                          <Pause size={12} className="text-[#FFD60A]" />
                        ) : (
                          <Play size={12} className="text-white/70 ml-0.5" />
                        )}
                      </div>
                      <div className="min-w-0">
                        <div className="text-[12px] font-semibold truncate text-white">{track.title}</div>
                        <div className="text-[10px] text-white/50 font-mono">{track.genre}</div>
                      </div>
                    </div>

                    <div className="flex items-center gap-2 shrink-0 ml-2">
                      {/* Active Soundwave Animation Bars */}
                      {isPlayingAudio && isCurrent && (
                        <div className="flex items-end gap-0.5 h-3.5">
                          <span className="w-0.5 bg-[#FFD60A] rounded-full soundwave-bar" />
                          <span className="w-0.5 bg-[#FFD60A] rounded-full soundwave-bar" />
                          <span className="w-0.5 bg-[#FFD60A] rounded-full soundwave-bar" />
                        </div>
                      )}
                      <span className="text-[10px] text-white/40 font-mono tabular-nums">
                        {track.duration}
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      )}

      {/* TAB 2: STORY SETTINGS with visionOS Sliders and Switches (From Image 3 & 4) */}
      {activeTab === 'settings' && (
        <div className="flex flex-col gap-3">
          <div className="vision-grouped-list p-4 flex items-center justify-between">
            <div>
              <div className="text-[13px] font-semibold text-white">VIP Istoriya Kloner Faol</div>
              <div className="text-[11px] text-white/50">Yangi e'lonlarni avtomatik istoriya qilish</div>
            </div>
            <Switch
              checked={Boolean(formData.is_active)}
              onChange={(val) => setFormData((prev) => ({ ...prev, is_active: val }))}
            />
          </div>

          {/* visionOS Capsule Slider for Min Price (From Image 3 & 4) */}
          <div className="vision-grouped-list p-4 flex flex-col gap-2.5">
            <CupertinoSlider
              label="Minimal E'lon Narxi"
              value={formData.min_price || 700}
              min={300}
              max={3000}
              step={50}
              displayValue={`$${formData.min_price || 700}`}
              onChange={(newVal) => setFormData((prev) => ({ ...prev, min_price: newVal }))}
            />
            <div className="flex items-center justify-between text-[10px] text-white/40 font-mono px-1">
              <span>$300</span>
              <span>$700 (Standart)</span>
              <span>$3,000+</span>
            </div>
          </div>

          {/* Background Style Pod */}
          <div className="vision-grouped-list p-4 flex flex-col gap-2.5">
            <div className="text-[13px] font-semibold text-white">Istoriya Fon Uslubi</div>
            <div className="grid grid-cols-2 gap-2">
              {BACKGROUND_STYLES.map((style) => (
                <button
                  key={style.id}
                  type="button"
                  onClick={() => {
                    telegram.selection();
                    setFormData((prev) => ({ ...prev, background_style: style.id }));
                  }}
                  className={`p-2.5 rounded-[16px] border text-xs font-semibold flex items-center gap-2 transition-all ${
                    formData.background_style === style.id
                      ? 'bg-white/10 border-[#FFD60A] text-white shadow-sm'
                      : 'bg-white/[0.03] border-white/5 text-white/50 hover:text-white'
                  }`}
                >
                  <span
                    className="w-3 h-3 rounded-full"
                    style={{ backgroundColor: style.color }}
                  />
                  <span>{style.label}</span>
                </button>
              ))}
            </div>
          </div>

          {/* Prime Hours Toggle */}
          <div className="vision-grouped-list p-4 flex items-center justify-between">
            <div>
              <div className="text-[13px] font-semibold text-white">Prime Hours (09:00 - 22:00)</div>
              <div className="text-[11px] text-white/50">Faqat faol vaqtda istoriya joylash</div>
            </div>
            <Switch
              checked={Boolean(formData.prime_hours_enabled)}
              onChange={(val) =>
                setFormData((prev) => ({ ...prev, prime_hours_enabled: val }))
              }
            />
          </div>

          {/* Smart Badges */}
          <div className="vision-grouped-list p-4 flex items-center justify-between">
            <div>
              <div className="text-[13px] font-semibold text-white">Smart Badges (Hashamatli Belgilar)</div>
              <div className="text-[11px] text-white/50">Narx, tuman va sifat ko'rsatkichlari</div>
            </div>
            <Switch
              checked={Boolean(formData.enable_smart_badges)}
              onChange={(val) =>
                setFormData((prev) => ({ ...prev, enable_smart_badges: val }))
              }
            />
          </div>

          {/* Save Button */}
          <button
            onClick={handleSave}
            disabled={isSaving}
            className="w-full py-3.5 vision-btn-gold flex items-center justify-center gap-2 text-[13px] font-bold active:scale-[0.97] transition-transform mt-2 disabled:opacity-50"
          >
            <Save size={16} />
            <span>{isSaving ? 'Saqlanmoqda...' : 'Sozlamalarni Saqlash'}</span>
          </button>
        </div>
      )}

      {/* TAB 3: QUEUE & POSTED */}
      {activeTab === 'queue' && (
        <div className="flex flex-col gap-3">
          <span className="text-[12px] font-semibold text-white/60 tracking-wider uppercase px-1">
            Render Navbatidagi E'lonlar
          </span>

          <div className="vision-grouped-list p-1 flex flex-col">
            {queue.length === 0 ? (
              <div className="p-6 text-center text-xs text-white/40">
                Hozirda render navbatida e'lonlar mavjud emas.
              </div>
            ) : (
              queue.map((item, idx) => (
                <React.Fragment key={item.id}>
                  {idx > 0 && <div className="vision-hairline" />}
                  <div className="p-3.5 flex items-center justify-between text-xs">
                    <div>
                      <div className="font-semibold text-white text-[13px]">{item.district}</div>
                      <div className="text-[11px] text-white/55 mt-0.5 font-mono">
                        {item.rooms} xona • {item.area} m² • ${item.price}
                      </div>
                    </div>
                    <span className="vision-btn-glass px-2.5 py-0.5 text-[10px] font-medium text-[#FFD60A] border-[#FFD60A]/30 bg-[#FFD60A]/10">
                      {item.scheduled_at}
                    </span>
                  </div>
                </React.Fragment>
              ))
            )}
          </div>

          <span className="text-[12px] font-semibold text-white/60 tracking-wider uppercase px-1 mt-2">
            Joylangan So'nggi Istoriyalar
          </span>

          <div className="vision-grouped-list p-1 flex flex-col">
            {posted.length === 0 ? (
              <div className="p-6 text-center text-xs text-white/40">
                Hozircha joylangan istoriyalar mavjud emas.
              </div>
            ) : (
              posted.map((item, idx) => (
                <React.Fragment key={item.id}>
                  {idx > 0 && <div className="vision-hairline" />}
                  <div className="p-3.5 flex items-center justify-between text-xs">
                    <div className="min-w-0 flex-1 mr-2">
                      <div className="font-semibold text-white truncate text-[13px]">{item.caption}</div>
                      <div className="text-[10px] text-white/40 font-mono mt-0.5">{item.posted_at}</div>
                    </div>
                    <span className="vision-btn-glass px-2.5 py-0.5 text-[10px] font-medium text-[#30D158] border-emerald-500/30 bg-emerald-500/10 shrink-0">
                      ${item.price} • Joylandi
                    </span>
                  </div>
                </React.Fragment>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
};
