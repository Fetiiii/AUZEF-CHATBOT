
module.exports = {
  darkMode: ['class'],
  content: ['./src/**/*.{html,ts}'],
  theme: {
    extend: {
      colors: {
        // AUZEF yeşil skalası — blue/indigo yerine (shade-for-shade uyumlu)
        brand: {50:'#F5F8EF',100:'#E9F1DC',200:'#D3E0BC',300:'#B4C892',400:'#93AE68',500:'#789650',600:'#61803E',700:'#536F35',800:'#445B2B',900:'#374B24',950:'#212E15'},
        // Semantik isimler (widget --c-* ile ortak roller)
        primary: {DEFAULT:'#536F35',dark:'#354923',light:'#789650',soft:'#EEF4E8'},
        gold:'#D8B86A',
        copper:'#B97942',
        ink:'#1F2A18',
        muted:'#6B755F',
        line:'#D8E1CF',
        sand:'#F5F1E8'
      },
      boxShadow: { card: '0 10px 30px -12px rgba(0,0,0,0.15)' }
    }
  },
  plugins: [require('@tailwindcss/forms'), require('@tailwindcss/typography'), require('@tailwindcss/aspect-ratio')],
}
