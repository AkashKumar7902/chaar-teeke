# Fonts

Self-hosted woff2, SIL Open Font License 1.1 (see `OFL.txt`). Downloaded on 19 Oct 2026 from the
Google Fonts CDN files named in the Google Fonts CSS API responses (`fonts.gstatic.com`). Each file is
one variable font that serves weights 400 to 700, so one request covers every weight.

| File | Family | Subset | Size | Source URL |
|---|---|---|---|---|
| `atkinson-latin.woff2` | Atkinson Hyperlegible Next | latin | 34 KB | https://fonts.gstatic.com/s/atkinsonhyperlegiblenext/v7/NaPNcYPdHfdVxJw0IfIP0lvYFqijb-UxCtm5_wdGseiJn3q0pkZ_.woff2 |
| `atkinson-latin-ext.woff2` | Atkinson Hyperlegible Next | latin-ext | 19 KB | https://fonts.gstatic.com/s/atkinsonhyperlegiblenext/v7/NaPNcYPdHfdVxJw0IfIP0lvYFqijb-UxCtm5_wdGseiHn3q0pkZ_S2Q.woff2 |
| `noto-sans-devanagari.woff2` | Noto Sans Devanagari | devanagari | 121 KB | https://fonts.gstatic.com/s/notosansdevanagari/v30/TuG7UUFzXI5FBtUq5a8bjKYTZjtRU6Sgv3NaV_SNmI0b8QQCQmHN5TV_5Kl4-GIB.woff2 |
| `noto-nastaliq-urdu-arabic.woff2` | Noto Nastaliq Urdu | arabic | 239 KB | https://fonts.gstatic.com/s/notonastaliqurdu/v23/LhW4MUPbN-oZdNFcBy1-DJYsEoTq5pu3SvoMGdX1OW12.woff2 |

Kept: latin, latin-ext, devanagari, arabic. Not kept: the Latin and Latin-extended slices of the two Noto
families. Latin letters and digits always come from Atkinson Hyperlegible Next (it is first in the font
stack), so those slices would never be used.

`@font-face` rules with `unicode-range` are in `web/css/tokens.css`. A page with no Urdu text never
downloads the Nastaliq file, and a page with no Hindi text never downloads the Devanagari file.
To update a font, replace the file, update this table and run the gallery check.
