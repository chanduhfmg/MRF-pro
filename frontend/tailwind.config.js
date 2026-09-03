/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: {
          950: "#0a0a0a",
          900: "#111213",
          850: "#161718",
          800: "#1c1d1f",
        },
        accent: {
          DEFAULT: "#1a5c38",
          50: "#e8f5ee",
          400: "#3ddc84",
          500: "#2f9e5f",
          600: "#1a5c38",
          700: "#134b2d",
        },
      },
      fontFamily: {
        sans: ['"Inter Variable"', "Inter", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
      },
      letterSpacing: {
        tightish: "-0.015em",
        tighter2: "-0.03em",
      },
      backdropBlur: {
        xs: "2px",
      },
      keyframes: {
        "aurora-1": {
          "0%,100%": { transform: "translate3d(-8%, -6%, 0) scale(1)" },
          "50%": { transform: "translate3d(10%, 8%, 0) scale(1.25)" },
        },
        "aurora-2": {
          "0%,100%": { transform: "translate3d(6%, 10%, 0) scale(1.1)" },
          "50%": { transform: "translate3d(-10%, -8%, 0) scale(0.9)" },
        },
        "aurora-3": {
          "0%,100%": { transform: "translate3d(0%, 4%, 0) scale(1.05)" },
          "50%": { transform: "translate3d(-6%, -10%, 0) scale(1.2)" },
        },
        shimmer: {
          "100%": { transform: "translateX(100%)" },
        },
        "pulse-dot": {
          "0%,100%": { opacity: "1", transform: "scale(1)" },
          "50%": { opacity: "0.35", transform: "scale(0.8)" },
        },
        "fade-up": {
          from: { opacity: "0", transform: "translateY(8px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
      },
      animation: {
        "aurora-1": "aurora-1 26s ease-in-out infinite",
        "aurora-2": "aurora-2 32s ease-in-out infinite",
        "aurora-3": "aurora-3 38s ease-in-out infinite",
        shimmer: "shimmer 1.8s infinite",
        "pulse-dot": "pulse-dot 1.4s ease-in-out infinite",
        "fade-up": "fade-up 0.4s ease-out both",
      },
    },
  },
  plugins: [],
};
