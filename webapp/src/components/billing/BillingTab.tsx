import React from 'react';
import { Crown, Check, Star, ShieldCheck, Zap } from 'lucide-react';
import { BillingCatalog, Subscription } from '../../types';
import { telegram } from '../../services/telegram';

interface BillingTabProps {
  subscription: Subscription | null;
  /** Prices from the server (config/plans.py); the static values below are only a fallback */
  billing: BillingCatalog | null;
  onSelectPlan: (planKey: string) => Promise<void>;
}

const periodLabel = (days: number) => (days % 30 === 0 ? `${days / 30} oy` : `${days} kun`);

export const BillingTab: React.FC<BillingTabProps> = ({ subscription, billing, onSelectPlan }) => {
  const currentTier = subscription?.tier || 'free';
  const [processingPlan, setProcessingPlan] = React.useState<string | null>(null);
  const planInfo = (key: 'pro' | 'vip') => billing?.plans.find((p) => p.key === key);
  const vipPlan = planInfo('vip');
  const proPlan = planInfo('pro');

  const plans = [
    {
      key: 'vip',
      name: 'VIP Cheksiz',
      stars: vipPlan?.stars ?? 300,
      period: periodLabel(vipPlan?.days ?? 30),
      badge: 'visionOS & Cheksiz',
      isPopular: false,
      isVip: true,
      iconColor: 'bg-[#FFD60A]',
      icon: Crown,
      features: [
        'Cheksiz kanallar klonlash (999+)',
        'VIP Real Estate Auto-Story Cloner ($700+)',
        '4K Playwright kollaj & Ken Burns video',
        'Telegram Premium animatsion emojilar',
        'Himoyalangan kanallar (Protected mode)',
        'AI Reklama Qalqoni (Ad Shield)',
        'Prioritet 24/7 server navbati',
      ]
    },
    {
      key: 'pro',
      name: 'Pro Tarif',
      stars: proPlan?.stars ?? 100,
      period: periodLabel(proPlan?.days ?? 30),
      badge: 'Eng Ommabop',
      isPopular: true,
      iconColor: 'bg-[#0A84FF]',
      icon: Zap,
      features: [
        `${proPlan?.max_channels ?? 5} tagacha faol kanal juftligi`,
        'AI Content Paraphraser (3 xil uslub)',
        'Rasm va Video Watermark (Logo urish)',
        'Avto-Tarjima (Uz, Ru, En, Tr)',
        'Dynamic Affiliate & Referal Almashtirgich',
        'Drip Feed kechikishi (0-45m)',
      ]
    },
    {
      key: 'free',
      name: 'Bepul Sinov',
      stars: 0,
      period: `${billing?.trial_days ?? 14} kun`,
      badge: 'Boshlang\'ich',
      isPopular: false,
      iconColor: 'bg-[#636366]',
      icon: ShieldCheck,
      features: [
        '1 ta faol kanal juftligi',
        'Begona link va reklamani tozalash',
        'Oddiy va Toza nusxa rejimi',
        'Shaxsiy imzo qo\'yish',
      ]
    }
  ];

  return (
    <div className="flex flex-col gap-4">
      <div className="animate-fade-up">
        <h2 className="text-[22px] font-bold text-white tracking-[-0.025em] flex items-center gap-2">
          <Crown size={22} className="text-[#FFD60A]" />
          <span>Tariflar & Obuna</span>
        </h2>
        <p className="text-[13px] text-white/55 mt-0.5">
          Bot imkoniyatlarini Telegram Stars orqali 1 soniyada faollashtiring.
        </p>
      </div>

      {/* Plans List with visionOS Cards and Colored Squircles */}
      <div className="flex flex-col gap-4">
        {plans.map((p, idx) => {
          const isCurrent = currentTier === p.key;
          const Icon = p.icon;

          return (
            <div
              key={p.key}
              className={`p-5 flex flex-col gap-3.5 relative overflow-hidden transition-all duration-200 animate-fade-up ${
                p.isVip
                  ? 'vision-intelligence-card liquid-glass-shimmer'
                  : 'vision-glass-panel'
              }`}
              style={{ animationDelay: `${idx * 0.06}s` }}
            >
              {/* Badge & Squircle Icon */}
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2.5">
                  <div className={`apple-squircle-badge ${p.iconColor} w-8 h-8 rounded-[9px] ${p.isVip ? 'text-black' : 'text-white'}`}>
                    <Icon size={16} className={p.isVip ? 'fill-black' : ''} />
                  </div>
                  <span
                    className={`px-3 py-1 rounded-full text-[10px] font-bold uppercase tracking-wider ${
                      p.isVip
                        ? 'bg-[#FFD60A]/20 text-[#FFD60A] border border-[#FFD60A]/40'
                        : p.isPopular
                        ? 'bg-[#0A84FF]/20 text-[#64D2FF] border border-[#0A84FF]/40'
                        : 'bg-white/10 text-white/70 border border-white/10'
                    }`}
                  >
                    {p.badge}
                  </span>
                </div>

                {isCurrent && (
                  <span className="text-[12px] font-semibold text-[#30D158] flex items-center gap-1.5">
                    <ShieldCheck size={14} /> Faol
                  </span>
                )}
              </div>

              {/* Title & Stars Price */}
              <div className="flex items-baseline justify-between pt-1">
                <h3 className="text-[20px] font-bold text-white tracking-tight">{p.name}</h3>
                <div className="flex items-baseline gap-1.5">
                  <span className={`text-[22px] font-extrabold font-mono tabular-nums ${p.isVip ? 'text-[#FFD60A]' : 'text-white'}`}>
                    {p.stars > 0 ? `${p.stars} Stars` : 'Bepul'}
                  </span>
                  <span className="text-[12px] text-white/50">/ {p.period}</span>
                </div>
              </div>

              {/* Features List */}
              <div className="flex flex-col gap-2 pt-2.5 border-t border-white/[0.08]">
                {p.features.map((feat, i) => (
                  <div key={i} className="flex items-center gap-2.5 text-[13px] text-white/85">
                    <Check
                      size={15}
                      className={p.isVip ? 'text-[#FFD60A] shrink-0' : 'text-[#30D158] shrink-0'}
                    />
                    <span>{feat}</span>
                  </div>
                ))}
              </div>

              {/* Action Button */}
              <button
                type="button"
                onClick={async () => {
                  telegram.impact('medium');
                  setProcessingPlan(p.key);
                  try {
                    await onSelectPlan(p.key);
                  } finally {
                    setProcessingPlan(null);
                  }
                }}
                disabled={processingPlan !== null}
                className={`w-full mt-2 py-3 px-4 text-[13px] font-semibold transition-all flex items-center justify-center gap-2 active:scale-[0.97] ${
                  p.isVip
                    ? 'vision-btn-gold'
                    : p.isPopular
                    ? 'vision-btn-blue'
                    : 'vision-btn-glass text-white/80'
                } ${processingPlan === p.key ? 'opacity-50 cursor-wait' : ''}`}
              >
                {processingPlan === p.key ? (
                  <span>Kuting...</span>
                ) : p.stars > 0 ? (
                  <>
                    <Star size={15} className="fill-current" />
                    <span>Telegram Stars bilan To'lash</span>
                  </>
                ) : (
                  <span>Tanlash</span>
                )}
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
};
