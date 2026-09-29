import React, { useState, useEffect, useMemo } from 'react';
import { ShoppingBag, Star, Search, AlertTriangle, CheckCircle, RefreshCw, Send, Sparkles } from 'lucide-react';
import { StoreProduct } from '../../types';
import { api, errorText, isAbortError } from '../../services/api';
import { telegram } from '../../services/telegram';
import { CupertinoSlider } from '../ui/CupertinoSlider';

const QUOTE_DEBOUNCE_MS = 350;

interface OrderResult {
  status: 'paid';
  message: string;
}

type QuoteState =
  | { state: 'loading' }
  | { state: 'ready'; price: number }
  | { state: 'error'; message: string };

/** Catalogue price as returned by the API (exactly what `display_quantity` units cost). */
function catalogPrice(p: StoreProduct): { stars: number; unitLabel: string } {
  const quantity = p.display_quantity || 1;
  const stars = Number.isFinite(p.display_price_stars)
    ? p.display_price_stars
    : Math.max(1, Math.ceil(p.price_stars * quantity));
  return { stars, unitLabel: quantity > 1 ? `/ ${quantity} ta` : '/ dona' };
}

export const StoreTab: React.FC = () => {
  const [products, setProducts] = useState<StoreProduct[]>([]);
  const [categories, setCategories] = useState<string[]>([]);
  const [selectedCategory, setSelectedCategory] = useState<string>('all');
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [buyingProduct, setBuyingProduct] = useState<StoreProduct | null>(null);
  const [targetLink, setTargetLink] = useState<string>('');
  const [quantity, setQuantity] = useState<number>(1);
  const [quote, setQuote] = useState<QuoteState>({ state: 'loading' });
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false);
  const [buyError, setBuyError] = useState<string>('');
  const [orderResult, setOrderResult] = useState<OrderResult | null>(null);

  const loadStoreData = async () => {
    setIsLoading(true);
    try {
      const [prodRes, catRes] = await Promise.allSettled([
        api.getStoreProducts(),
        api.getStoreCategories()
      ]);

      if (prodRes.status === 'fulfilled' && prodRes.value.products) {
        setProducts(prodRes.value.products);
      }
      if (catRes.status === 'fulfilled' && catRes.value.categories) {
        setCategories(catRes.value.categories);
      }
    } catch (e) {
      console.warn('Store load error:', e);
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    loadStoreData();
  }, []);

  // The total shown before paying is the server's quote (the same computation the invoice uses);
  // requests are debounced while the quantity slider moves and stale ones are cancelled.
  const buyingProductId = buyingProduct?.id;
  useEffect(() => {
    if (buyingProductId === undefined) return;
    const controller = new AbortController();
    setQuote({ state: 'loading' });
    const timer = setTimeout(() => {
      api.getStoreQuote(buyingProductId, quantity, controller.signal)
        .then((res) => setQuote({ state: 'ready', price: res.price_stars }))
        .catch((err) => {
          if (!isAbortError(err)) {
            setQuote({ state: 'error', message: errorText(err, "Narxni hisoblab bo'lmadi") });
          }
        });
    }, QUOTE_DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [buyingProductId, quantity]);

  const filteredProducts = useMemo(() => {
    return products.filter((p) => {
      const matchesCat = selectedCategory === 'all' || p.category === selectedCategory;
      const matchesSearch = !searchQuery || p.name.toLowerCase().includes(searchQuery.toLowerCase());
      return matchesCat && matchesSearch;
    });
  }, [products, selectedCategory, searchQuery]);

  const handleOpenBuy = (product: StoreProduct) => {
    if (product.stock_status === 'out_of_stock') {
      telegram.notification('warning');
      return;
    }
    telegram.impact('medium');
    setBuyingProduct(product);
    setQuantity(product.min_quantity || 1);
    setTargetLink('');
    setBuyError('');
    setOrderResult(null);
  };

  const handleConfirmBuy = async () => {
    if (!buyingProduct) return;
    if (!targetLink.trim()) {
      telegram.notification('error');
      setBuyError('Iltimos, kanal yoki profil havolasini kiriting!');
      return;
    }

    setIsSubmitting(true);
    setBuyError('');
    telegram.impact('heavy');
    try {
      const res = await api.buyStoreProduct(buyingProduct.id, quantity, targetLink.trim());
      telegram.openInvoice(res.invoice_link, (status: string) => {
        if (status === 'paid') {
          setOrderResult({
            status: 'paid',
            message: "To'lov qabul qilindi. Buyurtma holati bot orqali yuboriladi.",
          });
          telegram.notification('success');
          loadStoreData();
        } else if (status === 'failed') {
          telegram.notification('error');
          setBuyError("To'lov amalga oshmadi");
        }
      });
    } catch (err) {
      telegram.notification('error');
      setBuyError(errorText(err, 'Xarid qilishda xatolik yuz berdi'));
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      {/* visionOS Store Header */}
      <div className="vision-glass-panel p-5 relative overflow-hidden flex flex-col gap-3 animate-fade-up">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <div className="apple-squircle-badge bg-[#0A84FF] w-8 h-8 rounded-[9px]">
              <ShoppingBag size={16} className="text-white" />
            </div>
            <div>
              <h2 className="text-[20px] font-bold text-white tracking-tight flex items-center gap-1.5">
                <span>Do'kon & Xizmatlar</span>
                <Sparkles size={16} className="text-[#FFD60A]" />
              </h2>
              <p className="text-[12px] text-white/50">
                24/7 Avtomatlashtirilgan ta'minotchi API tizimi
              </p>
            </div>
          </div>

          <button
            onClick={() => {
              telegram.impact('light');
              loadStoreData();
            }}
            className="p-2 rounded-full bg-white/[0.08] hover:bg-white/[0.14] active:scale-95 transition-all text-white/70"
            title="Yangilash"
          >
            <RefreshCw size={15} className={isLoading ? 'animate-spin text-[#0A84FF]' : ''} />
          </button>
        </div>

        {/* Search Bar */}
        <div className="relative mt-1">
          <Search size={15} className="absolute left-3.5 top-1/2 -translate-y-1/2 text-white/40" />
          <input
            type="text"
            placeholder="Mahsulot yoki xizmatni qidirish..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="vision-input pl-10 pr-4 py-2.5 rounded-full text-xs placeholder:text-white/35 bg-white/[0.06] border-white/10 focus:border-[#0A84FF]"
          />
        </div>

        {/* Category Filter Pills */}
        <div className="flex items-center gap-1.5 overflow-x-auto no-scrollbar pt-1">
          <button
            onClick={() => {
              telegram.selection();
              setSelectedCategory('all');
            }}
            className={`px-3 py-1.5 rounded-full text-[11px] font-semibold transition-all whitespace-nowrap ${
              selectedCategory === 'all'
                ? 'bg-white/25 text-white shadow-sm'
                : 'bg-white/[0.06] text-white/55 hover:bg-white/10'
            }`}
          >
            Barchasi
          </button>
          {categories.map((cat) => (
            <button
              key={cat}
              onClick={() => {
                telegram.selection();
                setSelectedCategory(cat);
              }}
              className={`px-3 py-1.5 rounded-full text-[11px] font-semibold transition-all whitespace-nowrap ${
                selectedCategory === cat
                  ? 'bg-white/25 text-white shadow-sm'
                  : 'bg-white/[0.06] text-white/55 hover:bg-white/10'
              }`}
            >
              {cat}
            </button>
          ))}
        </div>
      </div>

      {/* Products Grid */}
      <div className="flex flex-col gap-3">
        {isLoading && products.length === 0 ? (
          <div className="text-center py-12 text-white/40 text-xs">
            <RefreshCw size={24} className="animate-spin text-[#0A84FF] mx-auto mb-2" />
            Mahsulotlar yuklanmoqda...
          </div>
        ) : filteredProducts.length === 0 ? (
          <div className="vision-glass-panel p-8 text-center text-white/40 text-xs">
            Hech qanday mahsulot topilmadi.
          </div>
        ) : (
          filteredProducts.map((p, idx) => {
            const isOutOfStock = p.stock_status === 'out_of_stock';
            const price = catalogPrice(p);

            return (
              <div
                key={p.id}
                className="vision-card p-4 flex flex-col gap-3 relative overflow-hidden animate-fade-up"
                style={{ animationDelay: `${idx * 0.04}s`, contain: 'layout style' }}
              >
                {/* Top Category & Stock Badge */}
                <div className="flex items-center justify-between">
                  <span className="text-[10px] font-bold uppercase tracking-wider text-white/50 bg-white/[0.08] px-2.5 py-0.5 rounded-full border border-white/10">
                    {p.category}
                  </span>

                  {isOutOfStock ? (
                    <span className="text-[10px] font-bold text-amber-400 bg-amber-400/15 border border-amber-400/30 px-2 py-0.5 rounded-full flex items-center gap-1">
                      <span className="w-1.5 h-1.5 rounded-full bg-amber-400" />
                      Vaqtincha tugadi
                    </span>
                  ) : (
                    <span className="text-[10px] font-bold text-[#30D158] bg-[#30D158]/15 border border-[#30D158]/30 px-2 py-0.5 rounded-full flex items-center gap-1">
                      <span className="w-1.5 h-1.5 rounded-full bg-[#30D158] animate-pulse" />
                      Mavjud (Avto)
                    </span>
                  )}
                </div>

                {/* Title & Description */}
                <div>
                  <h3 className="text-[15px] font-bold text-white tracking-tight leading-snug">
                    {p.name}
                  </h3>
                  {p.description && (
                    <p className="text-[11px] text-white/50 mt-1 line-clamp-2">
                      {p.description}
                    </p>
                  )}
                </div>

                {/* Price & Action Row */}
                <div className="flex items-center justify-between pt-2 border-t border-white/[0.08]">
                  <div className="flex items-baseline gap-1">
                    <span className="text-[18px] font-black font-mono text-[#FFD60A] tabular-nums">
                      {price.stars}
                    </span>
                    <span className="text-[11px] font-semibold text-white/60 flex items-center gap-0.5">
                      <Star size={11} className="fill-[#FFD60A] text-[#FFD60A]" /> Stars {price.unitLabel}
                    </span>
                  </div>

                  <button
                    type="button"
                    onClick={() => handleOpenBuy(p)}
                    disabled={isOutOfStock}
                    className={`py-2 px-4 rounded-full text-xs font-semibold flex items-center gap-1.5 transition-all active:scale-95 ${
                      isOutOfStock
                        ? 'bg-white/10 text-white/35 cursor-not-allowed'
                        : 'vision-btn-blue shadow-[0_4px_14px_rgba(10,132,255,0.4)]'
                    }`}
                  >
                    {isOutOfStock ? (
                      <span>Qolmadi</span>
                    ) : (
                      <>
                        <span>Sotib Olish</span>
                        <Send size={12} />
                      </>
                    )}
                  </button>
                </div>
              </div>
            );
          })
        )}
      </div>

      {/* Purchase Modal Sheet */}
      {buyingProduct && (
        <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center p-3 bg-black/75 backdrop-blur-md animate-fade-in">
          <div className="w-full max-w-md vision-glass-panel p-5 rounded-[28px] border border-white/20 shadow-2xl animate-slide-up flex flex-col gap-4">

            {/* Modal Header */}
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <div className="apple-squircle-badge bg-[#0A84FF] w-7 h-7 rounded-[8px]">
                  <ShoppingBag size={14} className="text-white" />
                </div>
                <h3 className="text-[16px] font-bold text-white tracking-tight">
                  Xaridni Tasdiqlash
                </h3>
              </div>
              <button
                onClick={() => setBuyingProduct(null)}
                className="w-7 h-7 rounded-full bg-white/10 flex items-center justify-center text-white/60 hover:text-white text-xs font-bold"
              >
                ✕
              </button>
            </div>

            {/* If Order Placed Result */}
            {orderResult ? (
              <div className="flex flex-col gap-3.5 py-2">
                <div className="p-4 rounded-[20px] bg-emerald-500/15 border border-emerald-500/30 flex flex-col gap-2 items-center text-center">
                  <CheckCircle size={32} className="text-[#30D158]" />
                  <h4 className="text-[15px] font-bold text-white">To'lov qabul qilindi!</h4>
                  <p className="text-[12px] text-white/70">
                    {orderResult.message}
                  </p>
                </div>

                <button
                  type="button"
                  onClick={() => setBuyingProduct(null)}
                  className="vision-btn-glass w-full py-2.5 rounded-full text-xs font-semibold text-white/80"
                >
                  Yopish
                </button>
              </div>
            ) : (
              /* Input Form */
              <div className="flex flex-col gap-3.5">
                <div className="p-3 rounded-[16px] bg-white/[0.05] border border-white/[0.08] flex flex-col gap-1">
                  <span className="text-[11px] text-white/50">Tanlangan mahsulot:</span>
                  <span className="text-[14px] font-bold text-white">{buyingProduct.name}</span>
                  <div className="flex items-center gap-1.5 text-xs text-[#FFD60A] font-bold mt-1">
                    <Star size={13} className="fill-[#FFD60A]" />
                    <span>
                      Jami narx: {quote.state === 'ready' ? `${quote.price} Stars` : '—'}
                      {quote.state === 'loading' && (
                        <RefreshCw size={11} className="inline-block ml-1.5 animate-spin text-white/50" />
                      )}
                    </span>
                  </div>
                  {quote.state === 'error' && (
                    <span className="text-[11px] text-amber-300/90 leading-relaxed">{quote.message}</span>
                  )}
                </div>

                {/* Target Link Input */}
                <div className="flex flex-col gap-1.5">
                  <label className="text-[11px] font-semibold text-white/70">
                    Havola (Kanal, Guruh yoki Post Linki):
                  </label>
                  <input
                    type="text"
                    placeholder="https://t.me/... yoki @kanal"
                    value={targetLink}
                    maxLength={255}
                    onChange={(e) => setTargetLink(e.target.value)}
                    className="vision-input text-xs"
                    autoFocus
                  />
                </div>

                {/* Quantity with 120 FPS CupertinoSlider */}
                {buyingProduct.max_quantity > 10 && (
                  <div className="pt-1">
                    <CupertinoSlider
                      label="Miqdor"
                      value={quantity}
                      min={buyingProduct.min_quantity || 1}
                      max={Math.min(1000, buyingProduct.max_quantity || 1000)}
                      step={buyingProduct.min_quantity > 1 ? buyingProduct.min_quantity : 1}
                      unit=" ta"
                      onChange={(val) => setQuantity(val)}
                    />
                  </div>
                )}

                {buyError && (
                  <div
                    role="alert"
                    className="p-3 rounded-[16px] bg-rose-950/70 border border-rose-500/40 text-rose-200 text-xs flex items-start gap-2"
                  >
                    <AlertTriangle size={14} className="text-rose-400 shrink-0 mt-0.5" />
                    <span className="leading-relaxed">{buyError}</span>
                  </div>
                )}

                {/* Action Buttons */}
                <div className="flex items-center gap-2 pt-2">
                  <button
                    type="button"
                    onClick={() => setBuyingProduct(null)}
                    className="vision-btn-glass flex-1 py-2.5 rounded-full text-xs font-semibold text-white/70"
                  >
                    Bekor qilish
                  </button>
                  <button
                    type="button"
                    onClick={handleConfirmBuy}
                    disabled={isSubmitting || !targetLink.trim()}
                    className={`vision-btn-blue flex-1 py-2.5 rounded-full text-xs font-bold flex items-center justify-center gap-1.5 ${
                      isSubmitting ? 'opacity-50 cursor-wait' : ''
                    }`}
                  >
                    {isSubmitting ? 'Bajarilmoqda...' : "Tasdiqlash & To'lash"}
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
};
