
module.exports = {
  darkMode: ['class'],
  content: ['./src/**/*.{html,ts}'],
  theme: {
    extend: {
      colors: {
        brand: {50:'#eef6ff',100:'#d9ecff',200:'#bfe0ff',300:'#92cbff',400:'#5dafee',500:'#2f90db',600:'#2078bc',700:'#1b5e94',800:'#1a4c74',900:'#173f61'}
      },
      boxShadow: { card: '0 10px 30px -12px rgba(0,0,0,0.15)' }
    }
  },
  plugins: [require('@tailwindcss/forms'), require('@tailwindcss/typography'), require('@tailwindcss/aspect-ratio')],
}
