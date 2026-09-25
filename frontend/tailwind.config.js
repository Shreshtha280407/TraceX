/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Shell (dark) -- two distinct dark tones, used deliberately.
        'shell-topbar': '#23201D', // Charcoal
        'shell-nav': '#19381F', // Evergreen
        'shell-nav-2': '#234A2B',
        'shell-text': '#FCE0E5',
        'shell-text-dim': '#A9B8A6',

        // Canvas & surfaces -- mid-tone, not stark light or dark.
        canvas: '#9EA3B0', // Cool Steel
        card: '#E7E6EC',
        'card-border': '#B9BDC7',

        // Text
        text: '#22242A',
        'text-dim': '#565A64',
        'text-faint': '#82868F',

        // Semantic accents -- meaning is fixed, do not swap roles.
        crimson: '#550527', // critical / primary
        berry: '#993955', // medium priority / hypothesis
        palm: '#7B904B', // verified / success
        'steel-neutral': '#5C6B78', // info / monitoring
      },
      borderRadius: {
        card: '10px',
        control: '7px', // buttons & inputs
        pill: '11px', // badges & pills
      },
      spacing: {
        18: '18px',
        22: '22px',
        26: '26px',
      },
      height: {
        topbar: '60px',
      },
      width: {
        sidebar: '224px',
      },
      fontFamily: {
        sans: [
          '-apple-system',
          'Segoe UI',
          'Roboto',
          'Helvetica',
          'Arial',
          'sans-serif',
        ],
        mono: [
          'ui-monospace',
          'SFMono-Regular',
          'Menlo',
          'Consolas',
          'monospace',
        ],
      },
    },
  },
  plugins: [],
}
