/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        canvas: {
          DEFAULT: '#000000',
          elevated: '#08080A',
        },
        apple: {
          blue: '#0A84FF',
          blueHover: '#0071E3',
          cyan: '#64D2FF',
          green: '#30D158',
          amber: '#FF9F0A',
          gold: '#FFD60A',
          purple: '#BF5AF2',
          red: '#FF453A',
          gray: '#8E8E93',
          gray2: '#636366',
          gray3: '#48484A',
          gray4: '#3A3A3C',
          gray5: '#2C2C2E',
          gray6: '#1C1C1E',
          label: '#FFFFFF',
          secondary: 'rgba(235, 235, 245, 0.60)',
          tertiary: 'rgba(235, 235, 245, 0.38)',
          quaternary: 'rgba(235, 235, 245, 0.18)',
        },
        surface: {
          base: 'rgba(28, 28, 30, 0.65)',
          card: 'rgba(38, 38, 42, 0.60)',
          hover: 'rgba(255, 255, 255, 0.08)',
          elevated: 'rgba(255, 255, 255, 0.10)',
        },
        ios: {
          blue: '#0A84FF',
          green: '#30D158',
          orange: '#FF9F0A',
          red: '#FF453A',
          gray: '#8E8E93',
        },
        vip: {
          gold: '#FFD60A',
        }
      },
      fontFamily: {
        sans: ['-apple-system', 'BlinkMacSystemFont', 'SF Pro Text', 'Inter', 'Segoe UI', 'Roboto', 'sans-serif'],
        display: ['-apple-system', 'BlinkMacSystemFont', 'SF Pro Display', 'Inter', 'Segoe UI', 'Roboto', 'sans-serif'],
        mono: ['SF Mono', 'ui-monospace', 'Menlo', 'Monaco', 'monospace'],
      },
      borderRadius: {
        'squircle-sm': '14px',
        'squircle-md': '20px',
        'squircle-lg': '26px',
        'squircle-xl': '32px',
        'squircle-2xl': '38px',
      },
      boxShadow: {
        'liquid': '0 12px 36px 0 rgba(0, 0, 0, 0.45), inset 0 1px 1px 0 rgba(255, 255, 255, 0.22)',
        'liquid-sm': '0 6px 20px 0 rgba(0, 0, 0, 0.35), inset 0 0.5px 0.5px 0 rgba(255, 255, 255, 0.20)',
        'liquid-pill': '0 4px 16px 0 rgba(0, 0, 0, 0.25), inset 0 0.75px 0.5px 0 rgba(255, 255, 255, 0.25)',
        'liquid-nav': '0 20px 48px 0 rgba(0, 0, 0, 0.75), inset 0 1px 1px 0 rgba(255, 255, 255, 0.20)',
        'apple-cta': '0 8px 24px 0 rgba(10, 132, 255, 0.38), inset 0 1px 0.5px 0 rgba(255, 255, 255, 0.45)',
        'gold-cta': '0 8px 24px 0 rgba(255, 214, 10, 0.35), inset 0 1px 0.5px 0 rgba(255, 255, 255, 0.50)',
        'card': '0 8px 30px rgba(0, 0, 0, 0.35)',
        'glass': '0 8px 32px 0 rgba(0, 0, 0, 0.4)',
        'elevated': '0 10px 40px -8px rgba(0, 0, 0, 0.7)',
      },
      animation: {
        'float': 'float 4s ease-in-out infinite',
        'aurora': 'auroraMove 18s ease-in-out infinite alternate',
        'shimmer': 'shimmer 2.5s cubic-bezier(0.4, 0, 0.6, 1) infinite',
      },
      keyframes: {
        float: {
          '0%, 100%': { transform: 'translateY(0px)' },
          '50%': { transform: 'translateY(-6px)' },
        },
        auroraMove: {
          '0%': { transform: 'translate(0, 0) scale(1) rotate(0deg)' },
          '50%': { transform: 'translate(3%, 4%) scale(1.12) rotate(3deg)' },
          '100%': { transform: 'translate(-3%, -2%) scale(1.05) rotate(-2deg)' },
        },
        shimmer: {
          '0%': { opacity: '0.4' },
          '50%': { opacity: '0.85' },
          '100%': { opacity: '0.4' },
        }
      },
      transitionTimingFunction: {
        'apple-spring': 'cubic-bezier(0.32, 0.72, 0, 1)',
      }
    },
  },
  plugins: [],
}
