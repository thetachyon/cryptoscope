"""Address risk scoring for the *Scope apps — keyless, self-contained.

Ports the Cryptanalysis forensics prototype's approach into Cryptoscope
without calling the live Cryptanalysis service (SSO-gated on Vercel)
and without any API key: a vendored OFAC SDN crypto-address label set,
heuristic scoring, and a bounded funder walk over the transactions the
address page already loads.

Label set (``OFAC_SDN``)
-------------------------
514 unique crypto addresses from the U.S. Treasury OFAC Specially
Designated Nationals (SDN) list — ``sdn.csv`` downloaded 2026-11-05
from https://www.treasury.gov/ofac/downloads/sdn.csv. The file carries
521 "Digital Currency Address - <CUR> <address>" mentions; 514 unique
addresses after normalisation/dedupe (a few addresses are listed under
two currencies/names). Mentions by currency: XBT 261, TRX 92, ETH 91,
USDT 43, LTC 8, XMR 6, DASH 5, ZEC 3, BCH 2, USDC 2, SOL 2, DOGE 2,
BTG 1, ETC 1, XVG 1, BNB 1. By address format: 94 EVM (``0x…``),
260 Bitcoin, 2 Solana; the rest are other chains (Tron, Monero, …)
and are kept in the set for reference — only EVM, Bitcoin and Solana
addresses are scored here. Program names are repaired only for the
CSV's dropped leading bracket (``HRIT-IR] [CYBER2`` ->
``[HRIT-IR] [CYBER2``); names and addresses are verbatim from OFAC.

Scoring
-------
- Direct SDN match: 100 / critical.
- N-hop proximity to a sanctioned address (funder walk): hop 1 = 80
  (high), hop 2 = 60 (elevated), hop 3 = 40 (elevated).
- Mixer interaction (counterparty is a sanctioned mixer — Blender.io
  or Sinbad, the mixer services that remain on the SDN list; 13
  addresses): hop 1 = 70, hop 2 = 50, hop 3 = 40 — always elevated,
  never high, because a mixer touch is weaker evidence than a direct
  sanctioned counterparty.
- Nothing found: 0 / low, with reasons that say what was checked.
Every response carries explicit human-readable reasons, never a bare
number, plus the coverage that produced it.

Honest limits (also stated in every API response and in the UI)
---------------------------------------------------------------
- A low score is NOT proof an address is clean: the walk only covers
  the transactions the address page loads (first page), capped as
  stated in each endpoint's coverage note.
- Solana: the SDN list holds only 2 Solana addresses, and the walk is
  1 hop only (counterparties from the 10 most recent transactions);
  deeper hops are not available via the public RPC in this version.
- Tornado Cash is NOT in the vendored set: OFAC removed it from the
  SDN list in March 2025, so it cannot appear as a match.
- Scores are a heuristic screening signal, not a legal determination.
"""

import asyncio
import re

OFAC_SDN_SOURCE = (
    "U.S. Treasury OFAC Specially Designated Nationals (SDN) list — "
    "sdn.csv, https://www.treasury.gov/ofac/downloads/sdn.csv"
)
OFAC_SDN_DATE = "2026-11-05"
OFAC_SDN_COUNT = 514

# Vendored label set: normalised address -> (currency, SDN name, program).
# EVM addresses are lowercased; all other formats are verbatim from OFAC.
# Generated from sdn.csv (2026-11-05); do not hand-edit entries.
SDN = {
    '0x0330070fd38ec3bb94f58fa55d40368271e9e54a': ('ETH', 'AMNOKGANG TECHNOLOGY DEVELOPMENT COMPANY', '[DPRK4]'),
    '0x038989cbb1710c72b9920dc4fa529158f463e72c': ('ETH', 'OJEDA AVILES, Armando de Jesus', '[SDGT] [ILLICIT-DRUGS-EO14059]'),
    '0x08723392ed15743cc38513c4925f5e6be5c17243': ('ETH', 'LAZARUS GROUP', '[DPRK3]'),
    '0x0931ca4d13bb4ba75d9b7132ab690265d749a5e7': ('ETH', 'CRYPTEX', '[CYBER2] [RUSSIA-EO14024]'),
    '0x098b716b8aaf21512996dc57eb0615e2383e2f96': ('ETH', 'LAZARUS GROUP', '[DPRK3]'),
    '0x12de548f79a50d2bd05481c8515c1ef5183666a9': ('ETH', 'OLD VECTOR LLC', '[CYBER4]'),
    '0x12ea80238c7d805d003ce793594e92d84a6f4e8d': ('ETH', 'BOYKOV, Ilya Aleksandrovich', '[DPRK2]'),
    '0x14779cec0b117d5194c750c55ea1f42086631964': ('ETH', 'OJEDA AVILES, Armando de Jesus', '[SDGT] [ILLICIT-DRUGS-EO14059]'),
    '0x175d44451403edf28469df03a9280c1197adb92c': ('USDT', 'GAZA NOW', '[SDGT]'),
    '0x1999ef52700c34de7ec2b68a28aafb37db0c5ade': ('ETH', 'MAGOMEDOV, Khadzhi Murat Dalgatovich', '[RUSSIA-EO14024]'),
    '0x19f8f2b0915daa12a3f5c9cf01df9e24d53794f7': ('ETH', 'OKO DESIGN BUREAU', '[RUSSIA-EO14024]'),
    '0x1b8579cf6ab12ea6b74ac5fa41f3829a3cb61e6e': ('ETH', "GHAL'EH-KUHI, Mojtaba", '[CYBER4]'),
    '0x1cab8177ace78b1b6b1c393371f4f2dcae40cbeb': ('ETH', 'MESRI, Behzad', '[HRIT-IR] [CYBER2]'),
    '0x1d19b52b54e7ef5ea1a4b40b616165e798eac9f8': ('ETH', 'RASHEVSKYI, Dmytro', '[CYBER4]'),
    '0x21b8d56bda776bbe68655a16895afd96f5534fed': ('ETH', 'GAZA NOW', '[SDGT]'),
    '0x252a8bd2319d8a555b872990601221b3a2053bce': ('ETH', 'MESRI, Behzad', '[HRIT-IR] [CYBER2]'),
    '0x2711d73d559f62f4f855ee21f38378f528e07985': ('ETH', 'FIRST VPN SERVICE', '[CYBER4]'),
    '0x2c7dcd774b33e10367f7d6385479e04f97d179dc': ('ETH', 'RASHEVSKYI, Dmytro', '[CYBER4]'),
    '0x32da24ca413f3e7b53145d4737e172c3bdf81e3e': ('ETH', 'OJEDA AVILES, Armando de Jesus', '[SDGT] [ILLICIT-DRUGS-EO14059]'),
    '0x35fb6f6db4fb05e6a4ce86f2c93691425626d4b1': ('ETH', 'LAZARUS GROUP', '[DPRK3]'),
    '0x38735f03b30fbc022ddd06abed01f0ca823c6a94': ('USDT', 'HANAFIN, John Desmond', '[RUSSIA-EO14024]'),
    '0x39d908dac893cbcb53cc86e0ecc369aa4def1a29': ('ETH', 'ZIMENKOV, Jonatan', '[RUSSIA-EO14024]'),
    '0x3ad9db589d201a710ed237c829c7860ba86510fc': ('ETH', 'TASK FORCE RUSICH', '[RUSSIA-EO14024]'),
    '0x3cbded43efdaf0fc77b9c55f6fc9988fcc9b757d': ('ETH', 'SOUTHFRONT', '[NPWMD] [CYBER2] [ELECTION-EO13848]'),
    '0x3cffd56b47b7b41c56258d9c7731abadc360e073': ('ETH', 'LAZARUS GROUP', '[DPRK3]'),
    '0x3e37627deaa754090fbfbb8bd226c1ce66d255e9': ('ETH', 'LAZARUS GROUP', '[DPRK3]'),
    '0x4060cbf80734193f521a3cc6fd4e985df2825279': ('ETH', 'BLAGH, Keyvan Fayyaz Gareh', '[CYBER4]'),
    '0x43fa21d92141ba9db43052492e0deee5aa5f0a93': ('ETH', 'SEMENOV, Roman', '[DPRK3]'),
    '0x4f428c11dc82388fa5136d636e613ad923eb700b': ('ETH', 'OJEDA AVILES, Armando de Jesus', '[SDGT] [ILLICIT-DRUGS-EO14059]'),
    '0x4f47bc496083c727c5fbe3ce9cdf2b0f6496270c': ('ETH', 'SIM, Hyon Sop', '[NPWMD]'),
    '0x530a64c0ce595026a4a556b703644228179e2d57': ('ETH', 'SHEN, Xingbiao', '[ILLICIT-DRUGS-EO14059]'),
    '0x532b77b33a040587e9fd1800088225f99b8b0e8a': ('ETH', 'ALIVAND, Arash Estaki', '[SDGT] [IFSR]'),
    '0x53b6936513e738f44fb50d2b9476730c0ab3bfc1': ('ETH', 'LAZARUS GROUP', '[DPRK3]'),
    '0x56de1527136f76a809e5b14ded6103eecd072ba7': ('ETH', 'BLAGH, Keyvan Fayyaz Gareh', '[CYBER4]'),
    '0x57ec89a0c056163a0314e413320f9b3abe761259': ('ETH', 'GARANTEX EUROPE OU', '[RUSSIA-EO14024] [CYBER4]'),
    '0x5a7a51bfb49f190e5a6060a5bc6052ac14a3b59f': ('ETH', 'SEMENOV, Roman', '[DPRK3]'),
    '0x5d5b5dafecbf31bdb08bfd3edad4f2694372d0ef': ('ETH', 'SOKOLOVSKI, Rolan', '[ILLICIT-DRUGS-EO14059]'),
    '0x5f48c2a71b2cc96e3f0ccae4e39318ff0dc375b2': ('ETH', 'SEMENOV, Roman', '[DPRK3]'),
    '0x6b69e2a7545c166417a80c61a77562052bffa9c5': ('ETH', 'SHPS SHELBIT', '[SDGT]'),
    '0x6be0ae71e6c41f2f9d0d1a3b8d0f75e6f6a0b46e': ('ETH', 'SEMENOV, Roman', '[DPRK3]'),
    '0x6fac4d18c912343bf86fa7049364dd4e424ab9c0': ('ETH', 'MESRI, Behzad', '[HRIT-IR] [CYBER2]'),
    '0x747afb5c7a7fc34b547cd0fdebf9b91759c5a52b': ('ETH', 'SIM, Hyon Sop', '[NPWMD]'),
    '0x761edd5e4546229a0bd35ae99b4d3d01cf4a3510': ('ETH', 'BOYKOV, Ilya Aleksandrovich', '[DPRK2]'),
    '0x76ea76ca4eb727f18956ab93445a94c5280412b9': ('ETH', 'SIM, Hyon Sop', '[NPWMD]'),
    '0x797d7ae72ebddcdea2a346c1834e04d1f8df102b': ('ETH', 'SEMENOV, Roman', '[DPRK3]'),
    '0x7ced75026204ac29c34bea98905d4c949f27361e': ('ETH', 'GARANTEX EUROPE OU', '[RUSSIA-EO14024] [CYBER4]'),
    '0x7f03679b56d8772530efa516b58bb83d4829e881': ('ETH', 'MESRI, Behzad', '[HRIT-IR] [CYBER2]'),
    '0x7f367cc41522ce07553e823bf3be79a889debe1b': ('ETH', 'POTEKHIN, Danil', '[CYBER2]'),
    '0x7ff9cfad3877f21d41da833e2f775db0569ee3d9': ('ETH', 'GARANTEX EUROPE OU', '[RUSSIA-EO14024] [CYBER4]'),
    '0x8ac5381fcd9e7395d14e02986c344aada84b4bc6': ('ETH', 'MESRI, Behzad', '[HRIT-IR] [CYBER2]'),
    '0x8d79c73daae8630c88de372ba8f57592fa987607': ('ETH', 'KAYVANPOUR, Siavash', '[SDGT] [IRGC]'),
    '0x8dce2aac0de82bdcaf6b4373b79f94331b8e4995': ('ETH', 'GARANTEX EUROPE OU', '[RUSSIA-EO14024] [CYBER4]'),
    '0x901bb9583b24d97e995513c6778dc6888ab6870e': ('ETH', 'LIFSHITS, Artem Mikhaylovich', '[CYBER2] [ELECTION-EO13848]'),
    '0x931546d9e66836abf687d2bc64b30407bac8c568': ('ETH', 'SEMENOV, Roman', '[DPRK3]'),
    '0x95584c303fcd48af5c6b9873015f2ad0ca84eae3': ('ETH', 'YUN, Song Guk', '[DPRK4]'),
    '0x961c5be54a2ffc17cf4cb021d863c42dacd47fc1': ('ETH', 'ZHANG, Wei', '[ILLICIT-DRUGS-EO14059]'),
    '0x97b1043abd9e6fc31681635166d430a458d14f9c': ('ETH', 'KIM, Sang Man', '[DPRK4]'),
    '0x983a81ca6fb1e441266d2fbcb7d8e530ac2e05a2': ('ETH', 'VALERIAN LABS, INC.', '[ILLICIT-DRUGS-EO14059]'),
    '0x9be599d7867f5e1a2d7ec6db9710df2b98a15573': ('ETH', 'AMNOKGANG TECHNOLOGY DEVELOPMENT COMPANY', '[DPRK4]'),
    '0x9c2bc757b66f24d60f016b6237f8cdd414a879fa': ('ETH', 'JIMENEZ CASTRO, Mario Alberto', '[ILLICIT-DRUGS-EO14059]'),
    '0x9f4cda013e354b8fc285bf4b9a60460cee7f7ea9': ('ETH', 'SOUTHFRONT', '[NPWMD] [CYBER2] [ELECTION-EO13848]'),
    '0xa0e1c89ef1a489c9c7de96311ed5ce5d32c20e4b': ('ETH', 'LAZARUS GROUP', '[DPRK3]'),
    '0xa40cfbfc8534ffc84e20a7d8bbc3729b26a35f6f': ('ETH', 'MESRI, Behzad', '[HRIT-IR] [CYBER2]'),
    '0xa7e5d5a720f06526557c513402f2e6b5fa20b008': ('ETH', 'LIFSHITS, Artem Mikhaylovich', '[CYBER2] [ELECTION-EO13848]'),
    '0xac4cc4b68ea24bbfaac8fd127b67ed445accce22': ('ETH', 'ALARCON PALOMARES, Rodrigo', '[ILLICIT-DRUGS-EO14059]'),
    '0xb338962b92cd818d6aef0a32a9ecd01212a71f33': ('ETH', 'GARANTEX EUROPE OU', '[RUSSIA-EO14024] [CYBER4]'),
    '0xb637f84b66876ebf609c2a4208905f9ddac9d075': ('ETH', 'YUN, Song Guk', '[DPRK4]'),
    '0xb6f5ec1a0a9cd1526536d3f0426c429529471f40': ('ETH', 'KIM, Sang Man', '[DPRK4]'),
    '0xbb69e01921b17cd22080968bcc96ba6115da6062': ('ETH', 'CRYPTO HOME DMCC', '[SDGT]'),
    '0xc103b7dc095c904b92081eef0c1640081ec01c10': ('ETH', 'SOKOLOVSKI, Rolan', '[ILLICIT-DRUGS-EO14059]'),
    '0xc2a3829f459b3edd87791c74cd45402ba0a20be3': ('ETH', 'TASK FORCE RUSICH', '[RUSSIA-EO14024]'),
    '0xcb74874f1e06fcf80a306e06e5379a44b488ba2d': ('ETH', 'AMNOKGANG TECHNOLOGY DEVELOPMENT COMPANY', '[DPRK4]'),
    '0xd04e33461fea8302c5e1e13895b60cee8aefda7f': ('ETH', 'SIM, Hyon Sop', '[NPWMD]'),
    '0xd5ed34b52ac4ab84d8fa8a231a3218bbf01ed510': ('ETH', 'FUNNULL TECHNOLOGY INC', '[CYBER3]'),
    '0xd8500c631dc32fa18645b7436344a99e4825e10e': ('ETH', 'GARANTEX EUROPE OU', '[RUSSIA-EO14024] [CYBER4]'),
    '0xd882cfc20f52f2599d84b8e8d58c7fb62cfe344b': ('ETH', 'KARASAVIDI, Dmitrii', '[CYBER2]'),
    '0xdb2720ebad55399117ddb4c4a4afd9a4ccada8fe': ('ETH', 'DERAKHSHAN, Alireza', '[SDGT] [IFSR]'),
    '0xdcbeffbecce100cce9e4b153c4e15cb885643193': ('ETH', 'SEMENOV, Roman', '[DPRK3]'),
    '0xe05f529f5284d75624eba386cb716928c3b54a2a': ('ETH', 'SHPS SHELBIT', '[SDGT]'),
    '0xe1e4c5e5ed8f03ae61b581e2def126025f2b9401': ('ETH', 'SOKOLOVSKI, Rolan', '[ILLICIT-DRUGS-EO14059]'),
    '0xe3d35f68383732649669aa990832e017340dbca5': ('ETH', 'ALIVAND, Arash Estaki', '[SDGT] [IFSR]'),
    '0xe4c9a7f68b1eba4508f54f07f7378847318dd1a3': ('ETH', 'KIM, Ki Hyok', '[DPRK2]'),
    '0xe950dc316b836e4eefb8308bf32bf7c72a1358ff': ('ETH', 'GAZA NOW', '[SDGT]'),
    '0xeb507efa9ee692a4c774ad1de9f3cb26fc459da3': ('ETH', 'KAHZADIAN, Arman', '[CYBER4]'),
    '0xed6e0a7e4ac94d976eebfb82ccf777a3c6bad921': ('ETH', 'SEMENOV, Roman', '[DPRK3]'),
    '0xef85a6fafa5942a964dc618e94e230881d29ce2a': ('ETH', 'KAHZADIAN, Arman', '[CYBER4]'),
    '0xf2235d55b2950a0b1317469d72d07ae65b2e27cb': ('ETH', 'OJEDA AVILES, Armando de Jesus', '[SDGT] [ILLICIT-DRUGS-EO14059]'),
    '0xf3701f445b6bdafedbca97d1e477357839e4120d': ('ETH', 'KONDRATIEV, Ivan Gennadievich', '[CYBER2]'),
    '0xf4377eda661e04b6dda78969796ed31658d602d4': ('ETH', 'GARANTEX EUROPE OU', '[RUSSIA-EO14024] [CYBER4]'),
    '0xf7b31119c2682c88d88d455dbb9d5932c65cf1be': ('ETH', 'LAZARUS GROUP', '[DPRK3]'),
    '0xfac583c0cf07ea434052c49115a4682172ab6b4f': ('USDT', 'WANG, Mingming', '[ILLICIT-DRUGS-EO14059]'),
    '0xfb3eff152ea55d1bfa04dbdd509a80fd7b72cdeb': ('ETH', 'SIM, Hyon Sop', '[NPWMD]'),
    '0xfda1ec4a6178d4916b001a065422d31ebe5f62ff': ('ETH', 'SIM, Hyon Sop', '[NPWMD]'),
    '0xfec8a60023265364d066a1212fde3930f6ae8da7': ('USDT', 'POLYANIN, Yevgeniy Igorevich', '[CYBER2]'),
    '125W5ek3DT6Zqy5S2iPt4FHQdNMCbZA3FU': ('XBT', 'WU, Huihui', '[DPRK3]'),
    '1295rkVyNfFpqZpXvKGhDqwhP1jZcNNDMV': ('XBT', 'SUEX OTC, S.R.O.', '[CYBER2]'),
    '129zKFLoVad9JtxSmDKeJoLCsjhGR7b3vr': ('XBT', 'PEIJNENBURG, Alex Adrianus Martinus', '[ILLICIT-DRUGS-EO14059]'),
    '12HQDsicffSBaYdJ6BhnE22sfjTESmmzKx': ('XBT', 'SUEX OTC, S.R.O.', '[CYBER2]'),
    '12NpCkhddSNiDkD9rRYUCHsTT9ReMNiJjG': ('XBT', 'GRIMM, Matthew Simon', '[ILLICIT-DRUGS-EO14059]'),
    '12QtD5BFwRsdNsAZY76UVE1xyCGNTojH9h': ('XBT', 'YAN, Xiaobing', '[SDNTK]'),
    '12YCfVAEzkEZXBYhUTyJJaRkgMXiFxJgcu': ('XBT', 'WU, Huihui', '[DPRK3]'),
    '12YyR9EpvHxBjjKjTWqfKqeyoWnvcraxpW': ('XBT', 'PEIJNENBURG, Alex Adrianus Martinus', '[ILLICIT-DRUGS-EO14059]'),
    '12aNKp2iDKuhEde2YfPdd4DFGenRUTKupL': ('XBT', 'MESRI, Behzad', '[HRIT-IR] [CYBER2]'),
    '12udabs2TkX7NXCSj6KpqXfakjE52ZPLhz': ('XBT', 'LIFSHITS, Artem Mikhaylovich', '[CYBER2] [ELECTION-EO13848]'),
    '12w6v1qAaBc4W8h8C2Cu5SKFaKDSv3erUW': ('XBT', 'WU, Huihui', '[DPRK3]'),
    '134r8iHv69xdT6p5qVKTsHrcUEuBVZAYak': ('XBT', 'TIAN, Yinyin', '[DPRK3] [CYBER2]'),
    '13JtX4h7G5ZuNK5mFudKGq9DHLvvMFuNuz': ('XBT', 'CRYPTEX', '[CYBER2] [RUSSIA-EO14024]'),
    '13RH4JaFhaCxDGPyYE9emjp2aDxdX18uBA': ('XBT', 'PEIJNENBURG, Alex Adrianus Martinus', '[ILLICIT-DRUGS-EO14059]'),
    '13YBQr2Cp1YY3xqq2qngaPb7ca1o4ugeq6': ('XBT', 'ANDREYEV, Anton Nikolaeyvich', '[CYBER2] [ELECTION-EO13848]'),
    '13f59kUM5FU8MfTG7DCEugYarDhSD7XCoC': ('XBT', 'YAN, Xiaobing', '[SDNTK]'),
    '13fhnkmpBBWXUQucJd6efWvXdEj78DKavk': ('XBT', 'OKO DESIGN BUREAU', '[RUSSIA-EO14024]'),
    '13mnk8SvDGqsQTHbiGiHBXqtaQCUKfcsnP': ('XBT', 'SUEX OTC, S.R.O.', '[CYBER2]'),
    '149w62rY42aZBox8fGcmqNsXUzSStKeq8C': ('XBT', 'KHORASHADIZADEH, Ali', '[CYBER2]'),
    '14gM1HuLVDELNHaFU22qpabjtiWek4HhV1': ('XBT', 'HYDRA MARKET', '[CYBER2]'),
    '14kqryJUxM3a7aEi117KX9hoLUw592WsMR': ('XBT', 'TIAN, Yinyin', '[DPRK3] [CYBER2]'),
    '14rjAD8ZP5xaL571cMRE98qgxxbg1S8mAN': ('XBT', 'WU, Huihui', '[DPRK3]'),
    '158treVZBGMBThoaympxccPdZPtqUfYrT9': ('XBT', 'POLYANIN, Yevgeniy Igorevich', '[CYBER2]'),
    '15Pt4NwZaUmMUwS2bQbyyncc7mzgWShtv8': ('XBT', 'ANDREYEV, Anton Nikolaeyvich', '[CYBER2] [ELECTION-EO13848]'),
    '15YK647qtoZQDzNrvY6HJL6QwXduLHfT28': ('XBT', 'TIAN, Yinyin', '[DPRK3] [CYBER2]'),
    '15Zt2mKuQ5Qy6DEXfDcbtsEVneoGQQXrS4': ('XBT', 'KAYVANPOUR, Siavash', '[SDGT] [IRGC]'),
    '15cRqR3TXS1JehBGWERuxFE8NhWZzfoeeU': ('XBT', 'KONDRATIEV, Ivan Gennadievich', '[CYBER2]'),
    '15kZobLkD6HZgEECtz4oS2Vz21XHTnNfSg': ('XBT', 'WU, Huihui', '[DPRK3]'),
    '15qyVrZvvVGvB7GWiAZ82TNcZ6QWMKu3kx': ('XBT', 'WU, Huihui', '[DPRK3]'),
    '15uqdxqNXQwVf5H7yZPz4TmEGeSccCwdor': ('XBT', 'GRIMM, Matthew Simon', '[ILLICIT-DRUGS-EO14059]'),
    '15yqWQ4sqr7jzCwDtZ3U1KaCa8WMEy7Mm2': ('XBT', 'GRIMM, Matthew Simon', '[ILLICIT-DRUGS-EO14059]'),
    '16PhXY3hNNMTo8kpuJx2emh713KbWpkqci': ('XBT', 'SECONDEYE SOLUTION', '[CYBER2] [ELECTION-EO13848]'),
    '17UVSMegvrzfobKC82dHXpZLtLcqzW9stF': ('XBT', 'LI, Jiadong', '[DPRK3] [CYBER2]'),
    '17V7THwHMiDJmDwZK4unhE5HgKFJKx7VCe': ('XBT', 'HYDRA MARKET', '[CYBER2]'),
    '17ezuJoT3XBbdcwFZbkTnrXbup11F4uhiy': ('XBT', 'ZHENG, Fujing', '[SDNTK]'),
    '17mhyeBX617ABZ1ffThhUTJkHUcMvCkfd5': ('XBT', 'HYDRA MARKET', '[CYBER2]'),
    '182NGZbPJXwg2WDrhrPpR7tpiGQkNPF844': ('XBT', 'SECONDEYE SOLUTION', '[CYBER2] [ELECTION-EO13848]'),
    '18Qj1THHuETfYhuRDZycXJbWwDMGw73Poa': ('XBT', 'ANDREYEV, Anton Nikolaeyvich', '[CYBER2] [ELECTION-EO13848]'),
    '18dLDAWi8LmrHbEq3QzDJb9SLxCf4uimXB': ('XBT', 'VOLOSOVIK, Aleksandr Aleksandrovich', '[CAATSA - RUSSIA] [CYBER4]'),
    '18gaXypKj9M23S2zT9qZfL9iPbLFM372Q5': ('XBT', 'SUNGATOV, Artur Ravilevich', '[CYBER2]'),
    '18uKfaUjgG52rVeXEi3wxnveww7zZuECtE': ('XBT', 'ZHENG, Guanghua', '[SDNTK]'),
    '18yWCu6agTxYqAerMxiz9sgHrK3ViezzGa': ('XBT', 'WU, Huihui', '[DPRK3]'),
    '1986rYHckYbJpGQJy6ornuMyD2N5MTqwDt': ('XBT', 'WU, Huihui', '[DPRK3]'),
    '19D1iGzDr7FyAdiy3ZZdxMd6ttHj1kj6WW': ('XBT', 'BUY CASH MONEY AND MONEY TRANSFER COMPANY', '[SDGT]'),
    '19D8PHBjZH29uS1uPZ4m3sVyqqfF8UFG9o': ('XBT', 'SECONDEYE SOLUTION', '[CYBER2] [ELECTION-EO13848]'),
    '19GrL5jnUkGmHXVcraB1Etv5rXCANeLWpq': ('XBT', 'GRIMM, Matthew Simon', '[ILLICIT-DRUGS-EO14059]'),
    '1A7SKE2dQtezLktCY8peLsdAtkqxV9r1dC': ('XBT', 'KONDRATIEV, Ivan Gennadievich', '[CYBER2]'),
    '1AXUTu9y3H8w4wYx4BjyFWgRhZKDhmcMrn': ('XBT', 'TIAN, Yinyin', '[DPRK3] [CYBER2]'),
    '1AeSq93WDNdLoEJ92sex7T8xQZoYYm8BtS': ('XBT', 'ANDREYEV, Anton Nikolaeyvich', '[CYBER2] [ELECTION-EO13848]'),
    '1AjZPMsnmpdK2Rv9KQNfMurTXinscVro9V': ('XBT', 'GHORBANIYAN, Mohammad', '[CYBER2]'),
    '1AoxtfiBQ22DvbhqAN9Ctb8sULMRhrdwTr': ('XBT', 'ANDREYEV, Anton Nikolaeyvich', '[CYBER2] [ELECTION-EO13848]'),
    '1B11Ezqg3AXj': ('XBT', 'HYDRA MARKET', '[CYBER2]'),
    '1B64QRxf': ('XBT', 'SUEX OTC, S.R.O.', '[CYBER2]'),
    '1BUXxhcwzFukm9CUxnhag85sSXDkMRbXse': ('XBT', 'KAYVANPOUR, Siavash', '[SDGT] [IRGC]'),
    '1BiUFjzH6wsT73U3tfy4aXHCQsYQHzjk5h': ('XBT', 'RAZA, Mujtaba Ali', '[CYBER2] [ELECTION-EO13848]'),
    '1CF46Rfbp97absrs7zb7dFfZS6qBXUm9EP': ('XBT', 'SUEX OTC, S.R.O.', '[CYBER2]'),
    '1CPJak9ZyddbawMGJPyEhCiJLXXb4sYv8N': ('XBT', 'WU, Huihui', '[DPRK3]'),
    '1D6gG9iKEhPitTcWRJiniuj1jYM2hNeAfJ': ('XBT', 'KIM, Sang Man', '[DPRK4]'),
    '1DDA93oZPn7wte2eR1ABwcFoxUFxkKMwCf': ('XBT', 'KARASAVIDI, Dmitrii', '[CYBER2]'),
    '1DH2xDH7TngrDU6LXciprKCBKNcPA1xX8A': ('XBT', 'ZHENG, Fujing', '[SDNTK]'),
    '1DJoEMvp95yJYWyxAZy8DDBzuvjnrTVrsN': ('XBT', 'PEIJNENBURG, Alex Adrianus Martinus', '[ILLICIT-DRUGS-EO14059]'),
    '1DJoVLgn1foJHHngduRPJvRbwpaFEKxvxd': ('XBT', 'WU, Huihui', '[DPRK3]'),
    '1DT3tenf14cxz9WFNxmYrXFbB6TFiVWA9U': ('XBT', 'LIFSHITS, Artem Mikhaylovich', '[CYBER2] [ELECTION-EO13848]'),
    '1DYFJ6CuBvrxyoQSuBzVsNcetY9tvdsrag': ('XBT', 'ANDREYEV, Anton Nikolaeyvich', '[CYBER2] [ELECTION-EO13848]'),
    '1DbShx4r8i2XesthoDBf5EkYWz5dsKEusV': ('XBT', 'ANDREYEV, Anton Nikolaeyvich', '[CYBER2] [ELECTION-EO13848]'),
    '1DbvK8P6imBuLcwh2Vruis4xsUb8YAwJQF': ('XBT', 'PEIJNENBURG, Alex Adrianus Martinus', '[ILLICIT-DRUGS-EO14059]'),
    '1Df883c96LVauVsx9FEgnsourD8DELwCUQ': ('XBT', 'SUEX OTC, S.R.O.', '[CYBER2]'),
    '1DfyWkiXVVqWfcSduj23qTDis9kb2qvRDa': ('XBT', 'RASHEVSKYI, Dmytro', '[CYBER4]'),
    '1ECeZBxCVJ8Wm2JSN3Cyc6rge2gnvD3W5K': ('XBT', 'SUEX OTC, S.R.O.', '[CYBER2]'),
    '1EYitrwBYNWuTBcjZFbEUdqHppe2raLpaF': ('XBT', 'SECONDEYE SOLUTION', '[CYBER2] [ELECTION-EO13848]'),
    '1Edue8XZCWNoDBNZgnQkCCivDyr9GEo4x6': ('XBT', 'SUEX OTC, S.R.O.', '[CYBER2]'),
    '1EfMVkxQQuZfBdocpJu6RUsCJvenQWbQyE': ('XBT', 'LI, Jiadong', '[DPRK3] [CYBER2]'),
    '1EpMiZkQVekM5ij12nMiEwttFPcDK9XhX6': ('XBT', 'YAN, Xiaobing', '[SDNTK]'),
    '1F2Gdug9ib9NQMhKMGGJczzMk5SuENoqrp': ('XBT', 'TIAN, Yinyin', '[DPRK3] [CYBER2]'),
    '1FCNgfZWpYMeYhy9t18AAkTBu8AsdoAc1Z': ('XBT', 'KIM, Sang Man', '[DPRK4]'),
    '1FE2cuvkq8n5VGwj5hi8YYQxskwJpovPyV': ('XBT', 'PEIJNENBURG, Alex Adrianus Martinus', '[ILLICIT-DRUGS-EO14059]'),
    '1FRyL9gmFGbzfYDAB4iY9836DJe3KSnjP9': ('XBT', 'ANDREYEV, Anton Nikolaeyvich', '[CYBER2] [ELECTION-EO13848]'),
    '1FjubFHV4mpYjBmvjsEhZssyiiA4TNmnm2': ('XBT', 'GRIMM, Matthew Simon', '[ILLICIT-DRUGS-EO14059]'),
    '1Fz29BQp82pE3vXXcsZoMNQ3KSHfMzfMe3': ('XBT', 'ANDREYEV, Anton Nikolaeyvich', '[CYBER2] [ELECTION-EO13848]'),
    '1G1V1b9s9uPsFCNL15ibZk44xsVygKsP2u': ('XBT', 'CRYPTO HOME DMCC', '[SDGT]'),
    '1G6DuwDKNHiUWqks2Lgu44cesu7ffFbLK7': ('XBT', 'PEIJNENBURG, Alex Adrianus Martinus', '[ILLICIT-DRUGS-EO14059]'),
    '1G9A8WRjGXdnYY4TNEVRrcaHsMtana4ncF': ('XBT', 'PEIJNENBURG, Alex Adrianus Martinus', '[ILLICIT-DRUGS-EO14059]'),
    '1G9CKRHA3mx22DoT1QyNYrh85VSQ19Y1em': ('XBT', 'SECONDEYE SOLUTION', '[CYBER2] [ELECTION-EO13848]'),
    '1GXeCkFQq7SYo6B7wdLAPkkri6NFTFB7No': ('XBT', "GHAL'EH-KUHI, Mojtaba", '[CYBER4]'),
    '1GYuu9d5HPikafbys3k5Q3DRJq6debGsoB': ('XBT', 'HYDRA MARKET', '[CYBER2]'),
    '1Gekw8ACSs37oXcc5XQHvoux3iKoVFtpF4': ('XBT', 'ZSERVERS', '[CYBER3]'),
    '1Gq': ('XBT', 'SECONDEYE SOLUTION', '[CYBER2] [ELECTION-EO13848]'),
    '1H939dom7i4WDLCKyGbXUp3fs9CSTNRzgL': ('XBT', 'KHATIBI AGHADA, Ahmad', '[IRGC] [IFSR] [CYBER2]'),
    '1HmqvgSXUVES1jn4TQPzk9dxo1NE8wim2T': ('XBT', 'ANDREYEV, Vitaliy Sergeyevich', '[DPRK2]'),
    '1Hn9ErTCPRP6j5UDBeuXPGuq5RtRjFJxJQ': ('XBT', 'TIAN, Yinyin', '[DPRK3] [CYBER2]'),
    '1Hpj6qm9i7nMF3VkKfBFtjhEDpEjxHWvgv': ('XBT', 'GRIMM, Matthew Simon', '[ILLICIT-DRUGS-EO14059]'),
    '1J9oGoAiHeRfeMZeUnJ9W7RpV55CdKtgYE': ('XBT', 'SUEX OTC, S.R.O.', '[CYBER2]'),
    '1JGPiQGK78xTxAVoxA1MjK3Z15M84Rj6BL': ('XBT', 'KAHZADIAN, Arman', '[CYBER4]'),
    '1JHdQHkBZiim1cb4hyUh2PbzEbbg6z2TrF': ('XBT', 'SINBAD', '[DPRK3] [CYBER2]'),
    '1JPfVv5cWRLx1js9NWxg46dG2CGbeRz4th': ('XBT', 'HEBEI ATUN TRADING CO., LTD.', '[ILLICIT-DRUGS-EO14059]'),
    '1JPqJ8sxLBvdHBqMqSFBmUkoh2vAuCUNvs': ('XBT', 'SOKOLOVSKI, Rolan', '[ILLICIT-DRUGS-EO14059]'),
    '1JREJdZupiFhE7ZzQPtASuMCvvpXC7wRsC': ('XBT', 'YAN, Xiaobing', '[SDNTK]'),
    '1Js6goCey2NaqPQptiLANLQGuk4d6mowjP': ('XBT', 'PEIJNENBURG, Alex Adrianus Martinus', '[ILLICIT-DRUGS-EO14059]'),
    '1KSAbh5trMCTZwhiNsuUQvfTtSSTT8zqRk': ('XBT', 'RAZA, Mujtaba Ali', '[CYBER2] [ELECTION-EO13848]'),
    '1KUUJPkyDhamZXgpsyXqNGc3x1QPXtdhgz': ('XBT', 'SUEX OTC, S.R.O.', '[CYBER2]'),
    '1Kc6egXevyLEaeTxLFA1Zyw7GuhCN8jQtt': ('XBT', 'WU, Huihui', '[DPRK3]'),
    '1KctQENEX5QkQMpnMC3Zh9yRAzkMBLpPcr': ('XBT', 'PEIJNENBURG, Alex Adrianus Martinus', '[ILLICIT-DRUGS-EO14059]'),
    '1KgudqxMfYaGzqAA7MS4DcsqejtMteqhix': ('XBT', 'ANDREYEV, Anton Nikolaeyvich', '[CYBER2] [ELECTION-EO13848]'),
    '1Kuf2Rd8mDyAViwBozGTNYnvWL8uYFrkVo': ('XBT', 'YAN, Xiaobing', '[SDNTK]'),
    '1Kys8fqDen8NGFUJ6AFcXfFW5qquuTH4eh': ('XBT', 'POTEKHIN, Danil', '[CYBER2]'),
    '1L4ncif9hh9TnUveqWq77HfWWt6CJWtrnb': ('XBT', 'SUEX OTC, S.R.O.', '[CYBER2]'),
    '1LAh7PQwpd1uGiLHae5C5Xz9QXse3y2phq': ('XBT', 'GRIMM, Matthew Simon', '[ILLICIT-DRUGS-EO14059]'),
    '1LiNmTUPSJEd92ZgVJjAV3RT9BzUjvUCkx': ('XBT', 'SUEX OTC, S.R.O.', '[CYBER2]'),
    '1Ljk8RNNabkZ9bfDYQBn98XfFozJhTjqcZ': ('XBT', 'ZHDANOVA, Ekaterina Valeryevna', '[RUSSIA-EO14024]'),
    '1LpYKb3SXZPve9hsH2QEJZFX279wJVGowi': ('XBT', 'GRIMM, Matthew Simon', '[ILLICIT-DRUGS-EO14059]'),
    '1LrxsRd7zNuxPJcL5rttnoeJFy1y4AffYY': ('XBT', 'SUEX OTC, S.R.O.', '[CYBER2]'),
    '1M5N4sJ1NHb4fviLVZA5MZLKhLZqU4CPZz': ('XBT', 'ZSERVERS', '[CYBER3]'),
    '1MLPqwaFFUBZePTjpR3nxRoK19Cv9mPCc7': ('XBT', 'WANG, Mingming', '[ILLICIT-DRUGS-EO14059]'),
    '1MTndG4K51RRMvkzyvguaHnQpiMLnxFGzM': ('XBT', 'RASHEVSKYI, Dmytro', '[CYBER4]'),
    '1MkCnCa9agS5t6V1B15bzusBgYECB4LfWp': ('XBT', 'WU, Huihui', '[DPRK3]'),
    '1MnbhWe5wr7Ut45ReyQsm96PwnM9jD7KaH': ('XBT', 'ANDREYEV, Anton Nikolaeyvich', '[CYBER2] [ELECTION-EO13848]'),
    '1N6XqS': ('XBT', 'PEIJNENBURG, Alex Adrianus Martinus', '[ILLICIT-DRUGS-EO14059]'),
    '1NE2NiGhhbkFPSEyNWwj7hKGhGDedBtSrQ': ('XBT', 'SECONDEYE SOLUTION', '[CYBER2] [ELECTION-EO13848]'),
    '1NayLEVF3bEEbDtdF2Cwso1VdEtvVNh2qX': ('XBT', 'SECONDEYE SOLUTION', '[CYBER2] [ELECTION-EO13848]'),
    '1NuBZQXJPyYQGfoBib8wWBDpZmbtkJa5Ba': ('XBT', 'WU, Huihui', '[DPRK3]'),
    '1P3ZfGFLezzYGg9k5SVzQmnjyh7nrUmF2y': ('XBT', 'YAN, Xiaobing', '[SDNTK]'),
    '1PJp8diNa89cVHpiT1VPu7EQ8LxYM5HX6v': ('XBT', 'HYDRA MARKET', '[CYBER2]'),
    '1PfwHNxUnkpfkK9MKjMqzR3Xq3KCtq9u17': ('XBT', 'TIAN, Yinyin', '[DPRK3] [CYBER2]'),
    '1PhqQpaGCrqSxQ6QDXcv14QCd1U98Zp34E': ('XBT', 'ANDREYEV, Anton Nikolaeyvich', '[CYBER2] [ELECTION-EO13848]'),
    '1Q6saNmqKkyFB9mFR68Ck8F7Dp7dTopF2W': ('XBT', 'KARASAVIDI, Dmitrii', '[CYBER2]'),
    '1Q9UAQbcDezmyouFrzt94t4dSMxgsUfW1X': ('XBT', 'POTEKHIN, Danil', '[CYBER2]'),
    '3': ('XBT', 'CHATEX', '[CYBER2]'),
    '31p6woV4e55HUfC2aGynFhzQnGoJFW26cD': ('XBT', 'POLYANIN, Yevgeniy Igorevich', '[CYBER2]'),
    '32DaxSzUhLBHY2WGSWQYiBSHnRsfQZrrRp': ('XBT', 'BLENDER.IO', '[CYBER2]'),
    '32jgFkZsTEjMFaBvxJnYvJEeTNKTmq5b32': ('XBT', 'GRIMM, Matthew Simon', '[ILLICIT-DRUGS-EO14059]'),
    '32pTjxTNi7snk8sodrgfmdKao3DEn1nVJM': ('XBT', 'KONDRATIEV, Ivan Gennadievich', '[CYBER2]'),
    '32wdqwX3zCEX3DhAVEcKwXCEGdzgBnx1R9': ('XBT', 'CHATEX', '[CYBER2]'),
    '33Kja69SQVc8kozpoP7Qw6HFtGxHkiWzTz': ('XBT', 'ZHENG, Guanghua', '[SDNTK]'),
    '33xWfziVZesgo83U5izdNCBVTnrtBpSwK7': ('XBT', 'CHATEX', '[CYBER2]'),
    '347QFbejDBdMZFTxpmn6evvvqyXiqZTCd7': ('XBT', 'CHATEX', '[CYBER2]'),
    '34pFGsSYbWEritXncW9unZtQQE9dKSvKku': ('XBT', 'BLENDER.IO', '[CYBER2]'),
    '35KAdTa2vqnJzitF2xiUzZn1Gmcas2Y465': ('XBT', 'HYDRA MARKET', '[CYBER2]'),
    '35LScRJ8hzDvvWh9t9UA8bHGnGNVz3YEfa': ('XBT', 'HYDRA MARKET', '[CYBER2]'),
    '35QpLWYkvD3ALhjbge5bK2kd7HfHYcDMu3': ('XBT', 'VASINSKYI, Yaroslav', '[CYBER2]'),
    '35irs2AU6pVgenYbWvMRz22avDBLC9XMkd': ('XBT', 'CHEN, Zhi', '[TCO]'),
    '35qwVtMEohWDdBWRiCSR7azoP5cbY8SG1Q': ('XBT', 'HYDRA MARKET', '[CYBER2]'),
    '3685sEusmTwZBiKJ4cgV73EAhpVD1nbgbe': ('XBT', 'ZHDANOVA, Ekaterina Valeryevna', '[RUSSIA-EO14024]'),
    '36ia8qAE1k1PKQePdXENCm1YKLXxZyVnnc': ('XBT', 'CHEN, Zhi', '[TCO]'),
    '372Wk9NLrMkJzKgqJdatWJy4bYRfxFjgat': ('XBT', 'VASINSKYI, Yaroslav', '[CYBER2]'),
    '374RguZDMNW5Zh5ouW3KhKDcrerKusmsuk': ('XBT', 'CHEN, Zhi', '[TCO]'),
    '37fKFZQGMqdBkjSUub1jCDWGgSHwv9VxfZ': ('XBT', 'SOKOLOVSKI, Rolan', '[ILLICIT-DRUGS-EO14059]'),
    '386sPhHBbLidzPnoxUH74jiiYkHYtobefQ': ('XBT', 'CHEN, Zhi', '[TCO]'),
    '389Sft4nJFkPGhbagk9FN4jXncA9piYTuU': ('XBT', 'POLYANIN, Yevgeniy Igorevich', '[CYBER2]'),
    '38TftW1heYPqvKbiAhT4ttNBqkUPpooXad': ('XBT', 'XIA, Fengbing', '[ILLICIT-DRUGS-EO14059]'),
    '38ncxqt932N9CcfNfYuHGZgCyR85hDkWBW': ('XBT', 'BLENDER.IO', '[CYBER2]'),
    '39NG2LcGRHXxSr1irpEVnJMw4ydL231sEn': ('XBT', 'GRIMM, Matthew Simon', '[ILLICIT-DRUGS-EO14059]'),
    '39Te8MbphSgs7npDJPj2hbNzhke61NTcnB': ('XBT', 'POLYANIN, Yevgeniy Igorevich', '[CYBER2]'),
    '39eboeqYNFe2VoLC3mUGx4dh6GNhLB3D2q': ('XBT', 'LI, Jiadong', '[DPRK3] [CYBER2]'),
    '39fhoB2DohisGBbHvvfmkdPdShT75CNHdX': ('XBT', 'LI, Jiadong', '[DPRK3] [CYBER2]'),
    '39p8qWp1bkBNhi4vPpFTetKPtH7goqNDZf': ('XBT', 'ZHDANOVA, Ekaterina Valeryevna', '[RUSSIA-EO14024]'),
    '3AQSmMk5n3c6TKEg9B2WyzYAPm33gJJAA4': ('XBT', 'PEIJNENBURG, Alex Adrianus Martinus', '[ILLICIT-DRUGS-EO14059]'),
    '3AjyprBY5yhijiCjUC5NUJutGbwhd3AQdE': ('XBT', 'POLYANIN, Yevgeniy Igorevich', '[CYBER2]'),
    '3B7S6zrgxQVQUHTU8wstM23tB9afE7ojuX': ('XBT', 'SHEN, Xingbiao', '[ILLICIT-DRUGS-EO14059]'),
    '3BsyZ7qRFSi3NsaoV1Ff724qAgrEpjVUHm': ('XBT', 'VASINSKYI, Yaroslav', '[CYBER2]'),
    '3CuqLrZWn5oCe6DR': ('XBT', 'CHEN, Zhi', '[TCO]'),
    '3DNsaQnaUz7wkQny1ZDSmtz6QfbEShxoDD': ('XBT', 'POLYANIN, Yevgeniy Igorevich', '[CYBER2]'),
    '3E6ZCKRrsdPc35chA9Eftp1h3DLW18NFNV': ('USDT', 'GARANTEX EUROPE OU', '[RUSSIA-EO14024] [CYBER4]'),
    '3E6rY4dSCDW6y2bzJNwrjvTtdmMQjB6yeh': ('XBT', 'LI, Jiadong', '[DPRK3] [CYBER2]'),
    '3E7YbpXuhh3CWFks1jmvWoV8y5DvsfzE6n': ('XBT', 'CHATEX', '[CYBER2]'),
    '3EPqGUw2q89pwPZ1UF8FJspE2AyojSTjdu': ('XBT', 'BLENDER.IO', '[CYBER2]'),
    '3EeR8FbcPbkcGj77D6ttneJxmsr3Nu7KGV': ('XBT', 'LI, Jiadong', '[DPRK3] [CYBER2]'),
    '3EynPFpoGTPxU9m5bPaEDEUxUanzh7vgQP': ('XBT', 'CHEN, Zhi', '[TCO]'),
    '3F2sZ4jbhvDKQdGbHYPC6ZxFXEau2m5Lqj': ('XBT', 'TIAN, Yinyin', '[DPRK3] [CYBER2]'),
    '3F6bbvS1krsc1qR8FsbTDfYQyvkMm': ('XBT', 'BLENDER.IO', '[CYBER2]'),
    '3F6bbvS1krsc1qR8FsbTDfYQyvkMm3QvmR': ('XBT', 'BLENDER.IO', '[CYBER2]'),
    '3FfRvC3kSo2SxiQe5e7SSuNdegwgq8iusL': ('XBT', 'MISHIN, Alexander Igorevich', '[CYBER3]'),
    '3FoD1f6Tfnq3s8MYHgJqFPWv9cUrtUdBSv': ('XBT', 'CHEN, Zhi', '[TCO]'),
    '3GXdtA6kbb4M5aqzZm5qqxcFDFRMW8LqdJ': ('XBT', 'HYDRA MARKET', '[CYBER2]'),
    '3Gbs4rjcVUtQd8p3CiFUCxPLZwRqurezRZ': ('XBT', 'SOUTHFRONT', '[NPWMD] [CYBER2] [ELECTION-EO13848]'),
    '3H3rh85qPaGLy2w6618yZNaH7i8asHv46B': ('XBT', 'CHATEX', '[CYBER2]'),
    '3HQRveQzPifZorZLDXHernc5zjoZax8U9f': ('XBT', 'LI, Jiadong', '[DPRK3] [CYBER2]'),
    '3He6EyDaCUgmdr4GXqhxbeTQukaGLCByU2': ('XBT', 'GRIMM, Matthew Simon', '[ILLICIT-DRUGS-EO14059]'),
    '3HePkztPU6UsdENxnyQHFrdZA6eVKeMtvz': ('XBT', 'WANG, Mingming', '[ILLICIT-DRUGS-EO14059]'),
    '3HqA7i3ttECLvgqvq69HNxxUP5BL7Z5YgA': ('XBT', 'PEIJNENBURG, Alex Adrianus Martinus', '[ILLICIT-DRUGS-EO14059]'),
    '3JHMz3mTna1gVCZSPp8NgRFiY7phkv5mA8': ('XBT', 'BLENDER.IO', '[CYBER2]'),
    '3JXKQ81JzBqVbB8VHdV9Jtd7auWokkdPgY': ('XBT', 'LI, Jiadong', '[DPRK3] [CYBER2]'),
    '3K35dyL85fR9ht7UgzPfd1gLRRXQtNTqE3': ('XBT', 'BLENDER.IO', '[CYBER2]'),
    '3K4rjdh8A5yi6LWvft2rbmyZvqEbPSSSX4': ('XBT', 'HYDRA MARKET', '[CYBER2]'),
    '3K7PMJyMNVnxqsfpmK9r9nJDtzDw9wNwNV': ('XBT', 'CHATEX', '[CYBER2]'),
    '3KHfXU24Bt3YD5Ef4J7uNp2buCuhrxfGen': ('XBT', 'LI, Jiadong', '[DPRK3] [CYBER2]'),
    '3LbDu1rUXHNyiz4i8eb3KwkSSBMf7C583D': ('XBT', 'LI, Jiadong', '[DPRK3] [CYBER2]'),
    '3LhnVMcBq4gsR7aDaRr9XmUo17CuYBV4FN': ('XBT', 'BLENDER.IO', '[CYBER2]'),
    '3Lpoy53K625zVeE47ZasiG5jGkAxJ27kh1': ('XBT', 'GARANTEX EUROPE OU', '[RUSSIA-EO14024] [CYBER4]'),
    '3MKXKqPXf6VxTLkVSXuDKzaTiZZaz5AmiK': ('XBT', 'CHEN, Zhi', '[TCO]'),
    '3MN8nYo1tt5hLxMwMbxDkXWd7Xu522hb9P': ('XBT', 'LI, Jiadong', '[DPRK3] [CYBER2]'),
    '3MTRvM5QrYZHKo8gh5qKcrPK3RLjxcDCZE': ('XBT', 'BLENDER.IO', '[CYBER2]'),
    '3MTrJTFhYK9v1C6pjHtuweZSopfZa4b1wb': ('XBT', 'CHATEX', '[CYBER2]'),
    '3MkUNScqf21EcfWq6T4x2MFgBeSTqhB5t6': ('XBT', 'ZHENG, Guanghua', '[SDNTK]'),
    '3N6WeZ6i34taX8Ditser6LKWBcXmt2XXL4': ('XBT', 'LI, Jiadong', '[DPRK3] [CYBER2]'),
    '3N9YcPBDky9UsMx1RTk33tL4jDkZfSnsPk': ('XBT', 'CHATEX', '[CYBER2]'),
    '3NEcQKZ8RwL2eDNhmMPpZ4td9vPsQh9JwM': ('XBT', 'HUANG, Xiaojun', '[ILLICIT-DRUGS-EO14059]'),
    '3NNcbBcMZokPB2JC2dxYuZrpgcetvyGeJn': ('XBT', 'WANG, Jiantong', '[ILLICIT-DRUGS-EO14059]'),
    '3NQ1aa9ceirMJ1JvRq3eXefvXj1L639fzX': ('XBT', 'VASINSKYI, Yaroslav', '[CYBER2]'),
    '3NRJ8aXdUiZdHaiFX9ePX3DhGHzcEi14Fq': ('XBT', 'CHATEX', '[CYBER2]'),
    '3NU9zkD8CCVGVmYnNNSN7PbU9sSFQBm5UV': ('XBT', 'ZHANG, Wei', '[ILLICIT-DRUGS-EO14059]'),
    '3PKiHs4GY4rFg8dpppNVPXGPqMX6K2cBML': ('XBT', 'WANG, Hongfei', '[ILLICIT-DRUGS-EO14059]'),
    '3PiCnZrBvGfWAKQ9hr4cCpfaDjy64yNSpE': ('XBT', 'HYDRA MARKET', '[CYBER2]'),
    '3Q5dGfLKkWqWSwYtbMUyc8xGjN5LrRviK4': ('XBT', 'BLENDER.IO', '[CYBER2]'),
    '3Q8H2ZWMtc4R1M3mkmhnTjCoYKTeCFigDP': ('XBT', 'GAZA NOW', '[SDGT]'),
    '42RLPACwZPx3vYYmxSueqsogfynBDqXK298EDsNoyoHi': ('SOL', 'SOKOLOVSKI, Rolan', '[ILLICIT-DRUGS-EO14059]'),
    '44dZUJ7w1T3fKAvFW8XyXUVoAGSbFvXef2wcbnsjNKGWYorpLJBjth5VKSFhLGkpYKJb2J341tdZHBnbpv72WL7e8zuxfR2': ('XMR', 'ISIL KHORASAN', '[FTO] [SDGT]'),
    '45ezvPejNsKYntjhyyeb7fbfzi1sxYQozc5Ffseh8jPGW4JZQQVXykNTfK5uGFiij7Hp3qPv3T5HCR7F3Fnmz2mzE1aKQwo': ('XMR', 'ISIL KHORASAN', '[FTO] [SDGT]'),
    '47FVhkRkWgnLs81UBqgY8ui6axAB1627HJjkY2qPnWxMbGfx7387kcY6RzScHgGMQAaH6CBxKXAS4862tS2fhs9BRdmuyav': ('XMR', 'ISIL KHORASAN', '[FTO] [SDGT]'),
    '49HqitRzdnhYjgTEAhgGpCfsjdTeMbUTU6cyR4JV1R7k2Eej9rGT8JpFiYDa4tZM6RZiFrHmMzgSrhHEqpDYKBe5B2ufNsL': ('XMR', 'SOUTHFRONT', '[NPWMD] [CYBER2] [ELECTION-EO13848]'),
    '5be5543ff73456ab9f2d207887e2af87322c651ea1a873c5b25b7ffae456c320': ('XMR', 'KARASAVIDI, Dmitrii', '[CYBER2]'),
    '6wjqWWra8ombzaw6VHrG5xpQ972jCYF6bbHiFCbWmr4U': ('SOL', 'SHPS SHELBIT', '[SDGT]'),
    '884Bz8UH63aYsjVdkfWfScRYWZGGNbjFL7pztqvWNSrtYT4reFSwyvkCj9KEGUtheHhhMUj87ciTBFyzoesrMJ4L1FvSoxL': ('XMR', 'SOUTHFRONT', '[NPWMD] [CYBER2] [ELECTION-EO13848]'),
    'DFFJhnQNZf8rf67tYnesPu7MuGUpYtzv7Z': ('XVG', 'RAZA, Mujtaba Ali', '[CYBER2] [ELECTION-EO13848]'),
    'DHzAVdEoL3PjGeLWNdEJwwMA1CeQ9J9Cpo': ('DOGE', 'RASHEVSKYI, Dmytro', '[CYBER4]'),
    'DTqpKQ96rqkTvHcohQQd9BksmgRjasHgGo': ('DOGE', 'RASHEVSKYI, Dmytro', '[CYBER4]'),
    'GPwg61XoHqQPNmAucFACuQ5H9sGCDv9TpS': ('BTG', 'KARASAVIDI, Dmitrii', '[CYBER2]'),
    'LNwgtMxcKUQ51dw7bQL1yPQjBVZh6QEqsd': ('LTC', 'KARASAVIDI, Dmitrii', '[CYBER2]'),
    'LaizKtS5DUhPuP1nTQcc83MS7HwK6vk85z': ('LTC', 'ZHENG, Guanghua', '[SDNTK]'),
    'LbPAqHvemZBv3pvAqiAtDnZ3U1t6EziaL1': ('LTC', 'RASHEVSKYI, Dmytro', '[CYBER4]'),
    'LcP1DumXkNJbBtSYD3XxAfsJ2nZR5hLdpM': ('LTC', 'RASHEVSKYI, Dmytro', '[CYBER4]'),
    'LeKvNdNEzgQkzVVnRdV3fAu2DSF1nLsNw6': ('LTC', 'RAZA, Mujtaba Ali', '[CYBER2] [ELECTION-EO13848]'),
    'Leo3j36nn1JcsUQruytQhFUdCdCH5YHMR3': ('LTC', 'LIFSHITS, Artem Mikhaylovich', '[CYBER2] [ELECTION-EO13848]'),
    'M8yFL6SFC6TreATegTyuSYkDfDRbisdpT3': ('LTC', 'CRYPTEX', '[CYBER2] [RUSSIA-EO14024]'),
    'TA3941uFAvmVibSkQ6fMJXxmaSNovX86mz': ('USDT', 'CHEIL CREDIT BANK', '[DPRK4]'),
    'TA39q3p75XRSWYAEaSF7dANtyksoa3sLge': ('USDT', 'CHEIL CREDIT BANK', '[DPRK4]'),
    'TASWbk6X1wiTku5TMmMQYqYFvshVEtfJy8': ('TRX', 'ZEDCEX EXCHANGE LTD', '[SDGT] [IFSR] [IRAN-EO13902]'),
    'TAYhjpL8pPs8T84FSM329nffQpc6jD8GBM': ('TRX', 'GRINEX', '[CYBER4]'),
    'TAhHpxPRwo1Bmm1A9m51uZ31tcB7EPq7SY': ('TRX', 'AL-JEBOURI, Zaid Issam Ahmed', '[SDGT]'),
    'TAhwhFv3JpK39Nc2m8W5LPCcoTisutiRfp': ('TRX', 'BANK MARKAZI JOMHOURI ISLAMI IRAN', '[IRAN] [SDGT] [IRGC] [IFSR]'),
    'TAoLw5yD5XUoHWeBZRSZ1ExK9HMv2CiPvP': ('TRX', 'WEDDING, Ryan James', '[ILLICIT-DRUGS-EO14059]'),
    'TBATDh41qMQ1yeVYecneEvhpfayYmkAQWS': ('USDT', 'CHEIL CREDIT BANK', '[DPRK4]'),
    'TBEmt7kPSwAv6NJTYKNdBVW524bUfPwJpJ': ('TRX', 'HERNANDEZ GUERRERO, Anthony Wuiliam', '[SDGT] [TCO]'),
    'TBFW9gF4oDX5cG44gS7AoxQeujScmm3z6h': ('TRX', 'RASHEVSKYI, Dmytro', '[CYBER4]'),
    'TBKpozurdWLrbKkxtDqkveeq1dSB8Axj3V': ('TRX', 'XINBI GUARANTEE', '[TCO]'),
    'TBWRDpQsW1ZVPGGaBAwVLNb7iqmVBuM1nj': ('USDT', 'CHEIL CREDIT BANK', '[DPRK4]'),
    'TBXMiRqUp1XH1zLazWu8cWitMAScv4HsYq': ('TRX', 'ABDERRAHMANE, Miloud', '[SDGT]'),
    'TBYRhsZR7Mdn9ezpGVLwZiNVnGm8wZGLAD': ('USDT', 'CHEIL CREDIT BANK', '[DPRK4]'),
    'TBZefVsyQpzzxc2WSCLbZBECvxVdzGqdtC': ('USDT', 'CHEIL CREDIT BANK', '[DPRK4]'),
    'TBcLqqqyZjNj1ptuXFgj5H768NhNU5nDyn': ('TRX', 'SOKOLOVSKI, Rolan', '[ILLICIT-DRUGS-EO14059]'),
    'TBtRqkiFzyuwe8nSAGQghxygAfwAF5eERq': ('TRX', 'ISIL KHORASAN', '[FTO] [SDGT]'),
    'TBwghbQMsBC5xcUxE7ZpYXhfDMXZAfiFv6': ('USDT', 'CHEIL CREDIT BANK', '[DPRK4]'),
    'TC4VsFHJdZ66BdwobZxkudVZBVmQZgCP65': ('USDT', 'ANSARALLAH', '[FTO] [SDGT]'),
    'TC5UNu3LGrjjVCuLNDdVgjm4oL5cQAyFRZ': ('USDT', 'CHEIL CREDIT BANK', '[DPRK4]'),
    'TC8axQvzJEVR3NKN6mZnJtGy7537GEmh38': ('TRX', 'GRINEX', '[CYBER4]'),
    'TCA7AfTSuDmgYk2VaezfPuZF4Z4X8wxwcQ': ('USDT', 'CHEIL CREDIT BANK', '[DPRK4]'),
    'TCA9vmjsYw9MtPKEwRBtGhKFRfr4CLxJAv': ('TRX', 'ZEDCEX EXCHANGE LTD', '[SDGT] [IFSR] [IRAN-EO13902]'),
    'TCFD8N3vM5b4Gr5f1kkajQsodRVNyyAq1d': ('TRX', 'OLD VECTOR LLC', '[CYBER4]'),
    'TCUmMCHQEbFFdGvfdg2LeS64QfzUKP2AgW': ('TRX', 'CARDENAS ARZOLA, Eric Gabriel', '[SDGT] [TCO]'),
    'TCV4qdpTxoMyqkn7sSPeRrXmhH5JFxMvmT': ('TRX', 'OBERTAS, Vitold Viktorovich', '[DPRK2]'),
    'TCifMAMwst3oEJx8GaNfqZw75dkFUa8vjG': ('TRX', 'MARTINEZ ARMENTA, Carlos Javier', '[SDGT] [TCO]'),
    'TCqKhk7MpPnGH3UfygE1TRzUo92a5tuVy6': ('TRX', 'ISIL KHORASAN', '[FTO] [SDGT]'),
    'TCu5onCzXuqxjvVzdB2tR4FLuF66d4yRqf': ('TRX', 'SOKOLOVSKI, Rolan', '[ILLICIT-DRUGS-EO14059]'),
    'TCzq6m2zxnQkrZrf8cqYcK6bbXQYAfWYKC': ('TRX', 'ZEDCEX EXCHANGE LTD', '[SDGT] [IFSR] [IRAN-EO13902]'),
    'TDFj8tYzfLDkwEMo4MJ2DfrbpMztuCCnan': ('TRX', 'ABDERRAHMANE, Miloud', '[SDGT]'),
    'TDFtJtyLPgN3oWUoHh23oJox3T5V5nR11K': ('TRX', 'SAYYED, Sadiq Abbas Habib', '[ILLICIT-DRUGS-EO14059]'),
    'TDNKsLvsY2iSznyghddXz7ZDRc': ('USDT', 'CHEIL CREDIT BANK', '[DPRK4]'),
    'TDU61o35UEe5hdB3R16c116DrPSV4kiyKz': ('TRX', 'XINBI GUARANTEE', '[TCO]'),
    'TDdbRFoBTEmE3qiR69Y6rKRSG1hoF65QaE': ('USDT', 'CHIRKINYAN, Elena', '[RUSSIA-EO14024]'),
    'TDe2UNAvuUnTbbDo7518eMe3TXN5qJW8Ft': ('TRX', 'AMNOKGANG TECHNOLOGY DEVELOPMENT COMPANY', '[DPRK4]'),
    'TDprk9jeYPVm6khkBp1u7fwuyJcEJFrPWD': ('USDT', 'ANSARALLAH', '[FTO] [SDGT]'),
    'TDwN1Eq62bqvVhfGAjdRRJjE46jsi5KNMV': ('USDT', 'ANSARALLAH', '[FTO] [SDGT]'),
    'TDxZ1XTZCmqkJJW2eJT362z6omRchJyGBX': ('TRX', 'MARTINEZ PIRONA, Oscar Leonardo', '[SDGT] [TCO]'),
    'TEAqwfMhXLaomXhZ8KeMhx3njGmQEDnsUR': ('USDT', 'SHEN, Xingbiao', '[ILLICIT-DRUGS-EO14059]'),
    'TEECjwHqVG8sDmM3VtscjuCaRe5p351MU7': ('TRX', 'XINBI GUARANTEE', '[TCO]'),
    'TEEYCuGDyeNkuDj4u6GQRXxXo3Nh29r2vP': ('TRX', 'AMNOKGANG TECHNOLOGY DEVELOPMENT COMPANY', '[DPRK4]'),
    'TEFph7dZoUN5233cGEzF6XFwRpjPF8fQDS': ('USDT', 'GAMBASHIDZE, Ilya Andreevich', '[RUSSIA-EO14024]'),
    'TEcuHDQthTmULe8fFLUccBPpjfXaTmJuuD': ('TRX', 'GRINEX', '[CYBER4]'),
    'TFFvv7NAWmbcVfA7QN81mMvUC25TWj1WJx': ('USDT', "AL-JAMAL, Sa'id Ahmad Muhammad", '[SDGT] [IFSR]'),
    'TFQbqaNbmq2xsVor2NbufLkYZvxFC9wC7k': ('TRX', 'BANK MARKAZI JOMHOURI ISLAMI IRAN', '[IRAN] [SDGT] [IRGC] [IFSR]'),
    'TFdTr9C3BqQrzKBXqSxJfAZFTh8UwBAfSg': ('TRX', 'OKO DESIGN BUREAU', '[RUSSIA-EO14024]'),
    'TFksjBcHPYqgAax2USheQuaT1ydRKyZRdg': ('TRX', 'OBERTAS, Vitold Viktorovich', '[DPRK2]'),
    'TFwjPScaJRCbSWVAywE1S1WgaU': ('TRX', 'GARANTEX EUROPE OU', '[RUSSIA-EO14024] [CYBER4]'),
    'TG83Fk56S2mycAUMYHZyZp9wMg6DCf4bUn': ('TRX', 'XINBI GUARANTEE', '[TCO]'),
    'TGJVc32ig2u8tQsYMLE7KXHT5NDQroaVNU': ('USDT', 'GAZA NOW', '[SDGT]'),
    'TGUPpmW2bAnMCLe5ih2CFisfCHk4gTFDsx': ('USDT', 'ANSARALLAH', '[FTO] [SDGT]'),
    'TGXE9dGWawjfd3xqFSho1h1bRbRv9wUGrF': ('TRX', 'SIM, Hyon Sop', '[NPWMD]'),
    'TGckaiamj5NzaYx6Qp6Zu7kahuHArzUo99': ('TRX', 'GRINEX', '[CYBER4]'),
    'TGr2i8ZQiiMRjP3mWyQUH2MQBfKu8GJNqm': ('TRX', 'AL-JEBOURI, Zaid Issam Ahmed', '[SDGT]'),
    'TGsNFrgWfbGN2gX25Wcf8oTejtxtQkvmEx': ('TRX', 'ZEDCEX EXCHANGE LTD', '[SDGT] [IFSR] [IRAN-EO13902]'),
    'TH96tFMn8KGiYSLiwcV3E2UiaJc8jmcbz3': ('USDT', 'GAZA NOW', '[SDGT]'),
    'THJUJFyTnrBWb1ijWav7qfRBrjUuKgP49Z': ('TRX', 'AL-JEBOURI, Zaid Issam Ahmed', '[SDGT]'),
    'THh5woR8qfmDsNknQ3agPYzQSiRtMnKsTh': ('USDT', "AL-JAMAL, Sa'id Ahmad Muhammad", '[SDGT] [IFSR]'),
    'THwbVuBBb26abe5TrAsYUpYz9mhWnBppdz': ('TRX', 'GALEANO BAZURTO, Jose Dario', '[SDGT] [TCO]'),
    'TJCBpxZ3yC7C7oegSRZMFxBcscmUVeSA36': ('USDT', 'TSORIS, Almpertos', '[IRAN-EO13902]'),
    'TJdgB1k6ot3f2nLuZug6D8eD3HavTmzmSK': ('TRX', 'BANK MARKAZI JOMHOURI ISLAMI IRAN', '[IRAN] [SDGT] [IRGC] [IFSR]'),
    'TJjRAn9kLiyh8h6gjBjaYjkfDkskgZfyW9': ('TRX', 'CANELON AGUIRRE, Anibal Alexander', '[SDGT] [TCO]'),
    'TK3n8pGHvDTcK53mpfeDAMzdwjb22qvFU3': ('TRX', 'XINBI GUARANTEE', '[TCO]'),
    'TKRNCETxbiMmyyMraCkgdXKdLr1ZkmKxV5': ('TRX', 'XINBI GUARANTEE', '[TCO]'),
    'TL1k1U6SHohxBqb68kCodxHc9y2LXoDSep': ('TRX', 'GRINEX', '[CYBER4]'),
    'TLM3zA3EWycoDX4ZX4gKze7sgfbdkntTum': ('USDT', 'OBSHCHESTVO S OGRANICHENNOI OTVETSTVENNOSTYU KONSTRUKTORSKOE BYURO VOSTOK', '[RUSSIA-EO14024]'),
    'TLNRT524dzL5FF1nJHDhYEMFpeWjLjRbz1': ('USDT', "AL-JAMAL, Sa'id Ahmad Muhammad", '[SDGT] [IFSR]'),
    'TLoG3vbjDgqmTD5bM7w9rMgA6ysaNReuRy': ('TRX', 'AL-JEBOURI, Zaid Issam Ahmed', '[SDGT]'),
    'TLvuvpfBKdxddxSsJefeiGCe9eVY8HUroE': ('TRX', 'ZEDCEX EXCHANGE LTD', '[SDGT] [IFSR] [IRAN-EO13902]'),
    'TM6Kix9wH8cYQ4rLpFifsQ4ddH3rPX3f8p': ('USDT', 'ANSARALLAH', '[FTO] [SDGT]'),
    'TMGLqRQ4twjW8wJhVH1mQR7nUThpGHUsN3': ('USDT', 'GAMBASHIDZE, Ilya Andreevich', '[RUSSIA-EO14024]'),
    'TMStbg5fgb4uTV7fK1gEYF9hKAzP3siPsG': ('TRX', 'WANG, Jiantong', '[ILLICIT-DRUGS-EO14059]'),
    'TMuCgBejD5RsNANZdjGtaM3YyKGNgDoy7N': ('TRX', 'GRINEX', '[CYBER4]'),
    'TNTFhgFoKH4srBMiWbfrVFqP2AThSmdwf1': ('TRX', 'SIM, Hyon Sop', '[NPWMD]'),
    'TNZxGWCwvsHr6JxQxzoeDXV597Yf7Zb7nV': ('TRX', 'GRINEX', '[CYBER4]'),
    'TNiq9AXBp9EjUqhDhrwrfvAA8U3GUQZH81': ('TRX', 'BANK MARKAZI JOMHOURI ISLAMI IRAN', '[IRAN] [SDGT] [IRGC] [IFSR]'),
    'TNmRfnSUXZoWWzxcDDbf95eGQYXt1mJDt8': ('TRX', 'FUNNULL TECHNOLOGY INC', '[CYBER3]'),
    'TNrX2FwrHKoo4XACGkmSzqeK4pdnKYn6Z7': ('TRX', 'AMNOKGANG TECHNOLOGY DEVELOPMENT COMPANY', '[DPRK4]'),
    'TNuA5CQ6LB4jTHoNrjEeQZJmcmhQuHMbQ7': ('TRX', 'ZEDCEX EXCHANGE LTD', '[SDGT] [IFSR] [IRAN-EO13902]'),
    'TPDLpXxPcaSsupEZ3yrVksmNkYP5SLeKxu': ('TRX', 'SIM, Hyon Sop', '[NPWMD]'),
    'TPJ1JNX98MJpHueBJeF5SVSg85z8mYg1P1': ('TRX', 'WEDDING, Ryan James', '[ILLICIT-DRUGS-EO14059]'),
    'TPgxjjB6AJeMSQFngbUM6dMMxPHa9GEtYa': ('TRX', 'KAYVANPOUR, Siavash', '[SDGT] [IRGC]'),
    'TPyE2oSoaysrXfLzwf9wetBVr9JudLwjtD': ('TRX', 'AL-JEBOURI, Zaid Issam Ahmed', '[SDGT]'),
    'TQ5H49Wz3K57zNHmuXVp6uLzFwitxviABs': ('TRX', 'ALIVAND, Arash Estaki', '[SDGT] [IFSR]'),
    'TQ5mpZPbQMSq2s1vSM3RJgiB1AsTsiTpFZ': ('USDT', 'ANSARALLAH', '[FTO] [SDGT]'),
    'TQCBdU4K92oxLAkzwexZpqVZJhmPbtLYMm': ('TRX', 'OBERTAS, Vitold Viktorovich', '[DPRK2]'),
    'TQKoDq5aCrWAciiNtete5pDYVN38i4qFN1': ('TRX', 'BOYKOV, Ilya Aleksandrovich', '[DPRK2]'),
    'TRBACioxdrdsYEZHvJWiUDZcMdBPpEe5Ub': ('TRX', 'ZHANG, Wei', '[ILLICIT-DRUGS-EO14059]'),
    'TRakpsE1mZjCUMNPyozR4BW2ZtJsF7ZWFN': ('TRX', 'ALIVAND, Arash Estaki', '[SDGT] [IFSR]'),
    'TSbGiFzG9xFXUYgGA6Rw': ('TRX', 'ISIL KHORASAN', '[FTO] [SDGT]'),
    'TScXZTbDjjZ6a6jiC7dZjgCqfNo4vm7atV': ('TRX', 'AL-JEBOURI, Zaid Issam Ahmed', '[SDGT]'),
    'TSxAAo67VTDgKT537EVXxdogkJtk9c6ojz': ('TRX', 'DERAKHSHAN, Alireza', '[SDGT] [IFSR]'),
    'TTAHMdqoom4f2VTWniroPWQHcTRZ4caoH4': ('USDT', "AL-JAMAL, Sa'id Ahmad Muhammad", '[SDGT] [IFSR]'),
    'TTKnV2S1295UrPr7N67Tp9ykNL7xX2Z4Uj': ('TRX', 'XIA, Fengbing', '[ILLICIT-DRUGS-EO14059]'),
    'TTLRNgLpz5H5tLPuNU4FViUs7zmmAtyvzW': ('TRX', 'RASHEVSKYI, Dmytro', '[CYBER4]'),
    'TTS9o5KkpGgH8cK9LofLmMAPYb5zfQvSNa': ('TRX', 'ZEDCEX EXCHANGE LTD', '[SDGT] [IFSR] [IRAN-EO13902]'),
    'TTUDyVhhpCC1xJoPmWzdjLAzeoPwbSABdr': ('TRX', 'CRYPTEX', '[CYBER2] [RUSSIA-EO14024]'),
    'TTVJuXWCusAURrpNShiypauagEH1N4CrxV': ('TRX', 'CRYPTO HOME DMCC', '[SDGT]'),
    'TTWRf2MD6zJ26bS4uKCNrcp84aAr7AmTjm': ('TRX', 'XINBI GUARANTEE', '[TCO]'),
    'TTgcTTNbNuFdbrhvbjMZVrdU5KALyzDaPw': ('USDT', 'GAZA NOW', '[SDGT]'),
    'TTiDLWE6fZK8okMJv6ijg42yrH6W2pjSr9': ('TRX', 'BANK MARKAZI JOMHOURI ISLAMI IRAN', '[IRAN] [SDGT] [IRGC] [IFSR]'),
    'TTs7KRq39547ruAsxb9brAPuTLy7h3U6H1': ('TRX', 'ISIL KHORASAN', '[FTO] [SDGT]'),
    'TU4tDFRvcKhAZ1jdihojmBWZqvJhQCnJ4F': ('TRX', 'AEZA GROUP LLC', '[CAATSA - RUSSIA] [CYBER4]'),
    'TUCsTq7TofTCJRRoHk6RvhMoS2mJLm5Yzq': ('XBT', 'WANG, Mingming', '[ILLICIT-DRUGS-EO14059]'),
    'TUoixKFaWVsHxGWgnYyMzpkrT22YqU7VvH': ('TRX', 'ISIL KHORASAN', '[FTO] [SDGT]'),
    'TUuaxBAWfA5nmsqNfycxYrzEvz4a5GJMGY': ('TRX', 'FIRST VPN SERVICE', '[CYBER4]'),
    'TV5ZTpKDszLTF6XcMnPongS33pwBgF91by': ('USDT', "AL-JAMAL, Sa'id Ahmad Muhammad", '[SDGT] [IFSR]'),
    'TVNyvx2astt2AB1Us67ENjfMZeEXZeiuu6': ('TRX', 'WEDDING, Ryan James', '[ILLICIT-DRUGS-EO14059]'),
    'TVacWx7F5wgMgn49L5frDf9KLgdYy8nPHL': ('USDT', 'SHAFIU, Ali', '[SDGT]'),
    'TVgUsVzA7mpFExP4zS7HHbtKti6UuBDuwZ': ('TRX', 'AL-JEBOURI, Zaid Issam Ahmed', '[SDGT]'),
    'TVn4q7L4fKEPbygi7UHq1CKfZxtbeuMV5q': ('USDT', 'ANSARALLAH', '[FTO] [SDGT]'),
    'TVttWTHTCU9kULP51m9BaHpmCW8H1BJJNQ': ('TRX', 'ISIL KHORASAN', '[FTO] [SDGT]'),
    'TW5tokvhEfrb77z98Rc8HqbkzQJ6sxYtGX': ('TRX', 'XINBI GUARANTEE', '[TCO]'),
    'TWBAPzpPiZarfVsY2BLXeaLhNHurn4wkWG': ('USDT', "AL-LAW, Tawfiq Muhammad Sa'id", '[SDGT]'),
    'TWJmTGhquvdp1jhBmgeGxBBwefteGZUQYW': ('TRX', 'MEJIA CASTILLO, Alejandro', '[SDGT] [TCO]'),
    'TWPma8xH48AEN93x2krdukV9e1j6sPsBeS': ('TRX', 'XINBI GUARANTEE', '[TCO]'),
    'TWqcMjV7Wq2RHe2CSiKQHpkn6A7B2AWUPe': ('TRX', 'XINBI GUARANTEE', '[TCO]'),
    'TX5GV4DyfxNB3rPkzZJhmqZ1efVmL4rEqG': ('USDT', 'TASK FORCE RUSICH', '[RUSSIA-EO14024]'),
    'TXEaKf4rT3rMoD3Vuqnuhwr6vLq6BL5t33': ('USDT', 'ANSARALLAH', '[FTO] [SDGT]'),
    'TXEsK1sEsKjZ1xtHitnyAAoqw3WLdYdRNW': ('USDT', 'GAZA NOW', '[SDGT]'),
    'TXGHxdYbGy574z5hBu4LNzq9NzjZQ9bhUf': ('TRX', 'BANK MARKAZI JOMHOURI ISLAMI IRAN', '[IRAN] [SDGT] [IRGC] [IFSR]'),
    'TXHX2NUvcgft4zs6MkkUtdNVERPTavHDWN': ('TRX', 'XINBI GUARANTEE', '[TCO]'),
    'TXN3hVukebhyYR31YPCYd7aJAvmUeXu1ab': ('TRX', 'SHPS SHELBIT', '[SDGT]'),
    'TXmrnVgsqCMJncYHTXchuJ5z111BXSj4g3': ('TRX', 'BOYKOV, Ilya Aleksandrovich', '[DPRK2]'),
    'TYDUutYN4YLKUPeT7TG27Yyqw6kNVLq9QZ': ('TRX', 'ALIVAND, Arash Estaki', '[SDGT] [IFSR]'),
    'TYkE8VwX1SGHM3Q2CA29nka5U2F2khKAj5': ('TRX', 'XINBI GUARANTEE', '[TCO]'),
    'TYvjt4ZKfsipHjA52nzgjUjDBF622SLCih': ('TRX', 'XINBI GUARANTEE', '[TCO]'),
    'TZB4NrX7k9ZsV6PRc1GigAztLL8WHpLvwP': ('TRX', 'AMNOKGANG TECHNOLOGY DEVELOPMENT COMPANY', '[DPRK4]'),
    'XcuGqRfrR85zyDrzVr1gSL5RNjwwpbu2KS': ('DASH', 'RASHEVSKYI, Dmytro', '[CYBER4]'),
    'XnPFsRWTaSgiVauosEwQ6dEitGYXgwznz2': ('DASH', 'KARASAVIDI, Dmitrii', '[CYBER2]'),
    'XowUeMFa1FkEUnAHL78E5oqpezMfSB6xP1': ('DASH', 'RASHEVSKYI, Dmytro', '[CYBER4]'),
    'Xs3vzQmNvAxRa3Xo8XzQqUb3BMgb9EogF4': ('DASH', 'LIFSHITS, Artem Mikhaylovich', '[CYBER2] [ELECTION-EO13848]'),
    'XyARKoupuArYtToA2S6yMdnoquDCDaBsaT': ('DASH', 'ASSOCIATION FOR FREE RESEARCH AND INTERNATIONAL COOPERATION', '[UKRAINE-EO13661] [CYBER2] [ELECTION-EO13848]'),
    'bc1q2lpgjntr348pfvxhfy33ehmdzy3gmx8w4052z6': ('XBT', 'TASK FORCE RUSICH', '[RUSSIA-EO14024]'),
    'bc1q2we5eqjj8je6lz9xwjattpc3pn4jejc5h0s70f': ('XBT', 'CHEN, Zhi', '[TCO]'),
    'bc1q3q2lz3cmt4u2f49ugxz5q34xrs0m2z4w2gmkp5': ('XBT', 'PARSARAD, Behrouz', '[ILLICIT-DRUGS-EO14059]'),
    'bc1q3y5v2khlyvemcz042wl98dzflywr8ghglqws6s': ('XBT', 'KHATIBI AGHADA, Ahmad', '[IRGC] [IFSR] [CYBER2]'),
    'bc1q45cxufu7x6s5v9adzm9t38k80gwawse75xffwe': ('XBT', 'PARSARAD, Behrouz', '[ILLICIT-DRUGS-EO14059]'),
    'bc1q4ktvxxhhvf8qhs9qkdsm9arcxfmmeex6rl7n50': ('XBT', 'BLAGH, Keyvan Fayyaz Gareh', '[CYBER4]'),
    'bc1q4namcagg5wljs0u20z6h2sqgsq4q5lts8rt2rr': ('XBT', 'WANG, Yunhe', '[CYBER2]'),
    'bc1q4yzd2rjmshm8ucrwy6dzj5rna20j0zlf8hf8vz': ('XBT', 'ZSERVERS', '[CYBER3]'),
    'bc1q5jqgm7nvrhaw2rh2vk0dk8e4gg5g373g0vz07r': ('XBT', 'KONDRATIEV, Ivan Gennadievich', '[CYBER2]'),
    'bc1q86tl9255vg5wldamfymaaz36uqxzm30gs7fhkljvzdlt9t38s3lqgdwdfq': ('XBT', 'CHATEX', '[CYBER2]'),
    'bc1q8ew45w2agdffrnwp6adt2gqrc9n4mkev9ns29c': ('XBT', 'KONDRATIEV, Ivan Gennadievich', '[CYBER2]'),
    'bc1q90zrdysy4flyacw7hsury3ajs9yzwtwp6guqpypx94w0d3p58hysvz6pde': ('XBT', 'CHATEX', '[CYBER2]'),
    'bc1q9lvynkfpaw330uhqmunzdz6gmafsvapv7y3zty': ('XBT', 'NIKAEEN RAVARI, Amir Hossein', '[IRGC] [IFSR] [CYBER2]'),
    'bc1qagp0gy58v8hqvw4p2wsphcxg067rrppp45hexr': ('XBT', 'KONDRATIEV, Ivan Gennadievich', '[CYBER2]'),
    'bc1qardcxz845jw83vgfvcmewhuxa0ag9gchjwmcfd': ('XBT', 'VALERIAN LABS, INC.', '[ILLICIT-DRUGS-EO14059]'),
    'bc1qdnr88f4d2yqunnc4mjsguezm6g3mlwe44z5dw8': ('XBT', 'FIRST VPN SERVICE', '[CYBER4]'),
    'bc1qdpazd6smrkq5hmw6lupq98za2fqtgmq3azv3av': ('XBT', 'WANG, Yunhe', '[CYBER2]'),
    'bc1qdt3gml5z5n50y5hm04u2yjdphefkm0fl2zdj68': ('XBT', 'SUEX OTC, S.R.O.', '[CYBER2]'),
    'bc1qeth6n6ryxexvkx34wnx3nuynun4474h3j0gkhw': ('XBT', 'CHEN, Zhi', '[TCO]'),
    'bc1qf6zxs4lkp9umh6hf087mh70w0ymcyt8cunp58r': ('XBT', 'BLAGH, Keyvan Fayyaz Gareh', '[CYBER4]'),
    'bc1qfg4gfg0y6t6xjnpmlhuwx5k0wlw6nmfzxn2psc': ('XBT', 'ZIMENKOV, Jonatan', '[RUSSIA-EO14024]'),
    'bc1qj2ffj6pepc80sfqpx2k2pd3q278q72dpqv2ens': ('XBT', 'PARSARAD, Behrouz', '[ILLICIT-DRUGS-EO14059]'),
    'bc1qjwr3rgww7a8ut7a4x8jqh3vswcue3xvp6dm7qe': ('XBT', 'PARSARAD, Behrouz', '[ILLICIT-DRUGS-EO14059]'),
    'bc1qjzk63wwatq2f638psymq3qapn00aqgksyx56l3t8eg7zuuccwasq498khf': ('XBT', 'SHPS SHELBIT', '[SDGT]'),
    'bc1qkfqh5p2qsakseg5gnwen6zh5xf3elmku2xaup29mw3l0wnncy3es880dss': ('XBT', 'XIA, Fengbing', '[ILLICIT-DRUGS-EO14059]'),
    'bc1ql7dlyh8xz6tpqk92vztrhqh88dmjvcwrmsemrm': ('XBT', 'TASK FORCE RUSICH', '[RUSSIA-EO14024]'),
    'bc1qmzmx8res2fgzd2wttyvf2ajqqqtcnxxrgkqc2t': ('XBT', 'PARSARAD, Behrouz', '[ILLICIT-DRUGS-EO14059]'),
    'bc1qn6segn8km4nfdp9vueu6msfjsaxaqgun9h60n9': ('XBT', 'KONDRATIEV, Ivan Gennadievich', '[CYBER2]'),
    'bc1qnujzvts45qka3cr2eqqw8ur3q6g6s0ze2wlk5m': ('XBT', 'CHEN, Zhi', '[TCO]'),
    'bc1qnykkrlk67f9kdqvzyw2ndu3xmef5z2e52886yl': ('XBT', 'WANG, Yunhe', '[CYBER2]'),
    'bc1qpaly5nm7pfka9v92d6qvl4fc2l9xzee8a6ys3s': ('XBT', 'NIKAEEN RAVARI, Amir Hossein', '[IRGC] [IFSR] [CYBER2]'),
    'bc1qq7p0es3dv5hcynjjf40f2xjjr6qp5py47d2f6n847vduuq9gvnyq7y9ecd': ('XBT', 'SINBAD', '[DPRK3] [CYBER2]'),
    'bc1qr2pwmvkpq5tc5um2g9uh34yyu6wepwx89rts70': ('XBT', 'BLAGH, Keyvan Fayyaz Gareh', '[CYBER4]'),
    'bc1qr4ankqmvmrhce3ydvzse86dfx5s3zhehfr9tg9': ('XBT', 'FIRST VPN SERVICE', '[CYBER4]'),
    'bc1qsxf77cvwcd6jv6j8d8j3uhh4g0xqw4meswmwuc': ('XBT', 'NIKAEEN RAVARI, Amir Hossein', '[IRGC] [IFSR] [CYBER2]'),
    'bc1qtdupwe722vcc5e0vh94cgwfs0ep4gzwyfsf75q': ('XBT', 'WANG, Yunhe', '[CYBER2]'),
    'bc1qte9ret332gwrk6e7vqc87d807npzvdj5savg4h': ('XBT', 'WANG, Yunhe', '[CYBER2]'),
    'bc1qtnr3t28l': ('XBT', 'PARSARAD, Behrouz', '[ILLICIT-DRUGS-EO14059]'),
    'bc1qtrrfhr0f3ufsrjxfv8a7p3yuqj0spe4cm6vaws': ('XBT', 'WANG, Yunhe', '[CYBER2]'),
    'bc1qtsl3ufenrv3zgrgm9z8xarcx22x8mfztuartyn': ('XBT', 'WANG, Yunhe', '[CYBER2]'),
    'bc1qu5vngdtcx8nc9d68pq8nv7pzcjrswsy87s66gk': ('XBT', 'WANG, Yunhe', '[CYBER2]'),
    'bc1qv4krhj2qmpd9qz9xj9nhvj99fq8s9xsw05d485': ('XBT', 'WANG, Yunhe', '[CYBER2]'),
    'bc1qv7k70u2zynvem59u88ctdlaw7hc735d8xep9rq': ('XBT', 'SOUTHFRONT', '[NPWMD] [CYBER2] [ELECTION-EO13848]'),
    'bc1qvhnfknw852ephxyc5hm4q520zmvf9maphetc9z': ('XBT', 'KHOROSHEV, Dmitry Yuryevich', '[CYBER2]'),
    'bc1qvxlwpgcw260sc97a0u6dm0cjxv027clhd67fvx': ('XBT', 'PARSARAD, Behrouz', '[ILLICIT-DRUGS-EO14059]'),
    'bc1qw4cxpe6sxa5dg6sdwxjph959cw6yztrzl4r54s': ('XBT', 'SOUTHFRONT', '[NPWMD] [CYBER2] [ELECTION-EO13848]'),
    'bc1qw4fxztd5u3sl7vrcqwk2a8v5zh5dllvckx3tlt': ('XBT', 'CHEN, Zhi', '[TCO]'),
    'bc1qw7vfgv3r5vnehafl0y95sclg3uqsj87wxs9ad628yjjcq33cwessr6ndyw': ('XBT', 'CHATEX', '[CYBER2]'),
    'bc1qw8evftpgc8wsmkemd8yl5fg2husza5z802xaym': ('XBT', 'WANG, Yunhe', '[CYBER2]'),
    'bc1qwtswqzvaxl6lyw3keqqzwt8z937psta3wg9sjy': ('XBT', 'PARSARAD, Behrouz', '[ILLICIT-DRUGS-EO14059]'),
    'bc1qwz0l2ceddckwdy5rh6zyrm3x4ha6gn5f2da5zg': ('XBT', 'WANG, Yunhe', '[CYBER2]'),
    'bc1qx3e2axj3wsfn0ndtvlwmkghmmgm4583nqg8ngk': ('XBT', 'KHATIBI AGHADA, Ahmad', '[IRGC] [IFSR] [CYBER2]'),
    'bc1qx9upga7f09tsetqf78wa3qrmcjar58mkwz6ng6': ('XBT', 'KONDRATIEV, Ivan Gennadievich', '[CYBER2]'),
    'bc1qyqvyupkpl6tm60zdecef925y220ssjuagpxsm0': ('XBT', 'PARSARAD, Behrouz', '[ILLICIT-DRUGS-EO14059]'),
    'bc1qyy5pt5cx3zth8xlj92lq5y87dh8xv3nwgs4ncq': ('XBT', 'HOANG, Minh Quang', '[DPRK4]'),
    'bc1qz654h5qcqzalv6ltpxsk4ksdcc3e20wkedqntgnarv3a0t2e2p7scr48pl': ('XBT', 'SHPS SHELBIT', '[SDGT]'),
    'bnb136ns6lfw4zs5hg4n85vdthaad7hq5m4gtkgf23': ('BNB', 'CRYPTO HOME DMCC', '[SDGT]'),
    'ltc1qr8ntsedq8tv0svmxqhzvdcdl5k7kntdmnhwep7': ('LTC', 'FIRST VPN SERVICE', '[CYBER4]'),
    'qpf2cphc5dkuclkqur7lhj2yuqq9pk3hmukle77vhq': ('BCH', 'SOUTHFRONT', '[NPWMD] [CYBER2] [ELECTION-EO13848]'),
    'qzjv8hrdvz6edu4gkzpnd4w6jc7zf296g5e9kkq4lx': ('BCH', 'SOUTHFRONT', '[NPWMD] [CYBER2] [ELECTION-EO13848]'),
    't1LHesgnkapziGQCJtrfZWYXhyjfTVo1dvh': ('ZEC', 'RASHEVSKYI, Dmytro', '[CYBER4]'),
    't1MMXtBrSp1XG38Lx9cePcNUCJj5vdWfUWL': ('ZEC', 'ASSOCIATION FOR FREE RESEARCH AND INTERNATIONAL COOPERATION', '[UKRAINE-EO13661] [CYBER2] [ELECTION-EO13848]'),
    't1g7wowvQ8gn2v8jrU1biyJ26sieNqNsBJy': ('ZEC', 'KARASAVIDI, Dmitrii', '[CYBER2]'),
}

MIXER_NAME_KEYWORDS = ("BLENDER", "SINBAD")

# Hop scores: nearest sanctioned counterparty wins. Mixer counterparties
# score lower than other sanctioned counterparties at the same hop.
HOP_SCORES = {1: 80, 2: 60, 3: 40}
MIXER_HOP_SCORES = {1: 70, 2: 50, 3: 40}

FAMILY_COUNTS = {
    "evm": sum(1 for _k in SDN if _k.startswith("0x")),
    "bitcoin": sum(1 for _v in SDN.values() if _v[0] == "XBT"),
    "solana": sum(1 for _v in SDN.values() if _v[0] == "SOL"),
}

FAMILY_NOTES = {
    "evm": "The OFAC SDN list holds 94 EVM-format addresses (ETH/USDT). "
    "Direct matches and the funder walk are checked against those.",
    "bitcoin": "The OFAC SDN list holds 260 Bitcoin addresses. Direct "
    "matches and the funder walk are checked against those.",
    "solana": "The OFAC SDN list holds only 2 Solana addresses — Solana "
    "scoring is mainly a direct match against those two, plus a 1-hop "
    "check of recent-transaction counterparties (which can only match "
    "those same 2 addresses).",
}

COVERAGE_NOTES = {
    "evm": "Funder walk covers the first page of this address's "
    "transactions (up to 50, newest first) plus one page each for up to "
    "10 first-hop and 5 second-hop counterparties — 3 hops max. Older "
    "transactions are not checked.",
    "bitcoin": "Funder walk covers this address's 25 most recent "
    "transactions (one mempool.space page) plus one page each for up to "
    "10 first-hop and 5 second-hop counterparties — 3 hops max. Older "
    "transactions are not checked.",
    "solana": "Funder walk is 1 hop only: counterparties from the 10 "
    "most recent transactions (public Solana RPC). Deeper hops are not "
    "available on Solana in this version.",
}

METHOD_NOTE = (
    "Heuristic screening signal, not a legal determination. A low score "
    "does not prove an address is clean — it means no SDN match was "
    "found in the transactions checked."
)

# Well-known Solana program IDs, excluded from the counterparty set —
# they appear in nearly every transaction and are not counterparties.
KNOWN_PROGRAMS = {
    "11111111111111111111111111111111",  # System Program
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # SPL Token
    "TokenzQdBNbLqP5VEhdkAS6VT4S7DDsm1BffU",  # Token-2022
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",  # Associated Token
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",  # Memo
    "ComputeBudget111111111111111111111111111111",  # Compute Budget
    "AddressLookupTab1e1BNb6XbTtt3fCZ6sQ3p5E9p5f5t1XGq5k",  # Lookup Table
}


def _key(addr):
    a = (addr or "").strip()
    return a.lower() if a.startswith(("0x", "0X")) else a


def lookup(addr):
    """OFAC SDN entry for an address, or None. EVM lookup is case-insensitive."""
    e = SDN.get(_key(addr))
    if e is None:
        return None
    return {"currency": e[0], "name": e[1], "program": e[2]}


def is_mixer(entry):
    """True if an SDN entry is a sanctioned mixer service."""
    return any(w in (entry["name"] or "").upper() for w in MIXER_NAME_KEYWORDS)


def level_for(score):
    if score >= 90:
        return "critical"
    if score >= 75:
        return "high"
    if score >= 40:
        return "elevated"
    return "low"


def _short(addr):
    a = str(addr)
    return f"{a[:6]}…{a[-4:]}" if len(a) > 12 else a


def assess(address, family, hops, *, txs_scanned, hops_checked,
           counterparties_checked, walk_partial=False, walk_skipped=False):
    """Pure scoring over a hop map: {hop: [counterparty addresses]}.

    Returns the full API response dict: score 0-100, level, explicit
    reasons, hits, coverage, and the SDN source. Never a bare number.
    """
    direct = lookup(address)
    hits = []
    for hop in sorted(hops):
        for cp in hops[hop]:
            e = lookup(cp)
            if e is None:
                continue
            mixer = is_mixer(e)
            table = MIXER_HOP_SCORES if mixer else HOP_SCORES
            hits.append({
                "hop": hop,
                "address": cp,
                "currency": e["currency"],
                "name": e["name"],
                "program": e["program"],
                "mixer": mixer,
                "score": table[hop],
            })
    if direct is not None:
        score = 100
    elif hits:
        score = max(h["score"] for h in hits)
    else:
        score = 0
    level = level_for(score)

    reasons = []
    if direct is not None:
        reasons.append(
            f"This address is on the OFAC SDN list as {direct['name']} "
            f"({direct['program']}; {direct['currency']} address). "
            "Direct sanctions match."
        )
    for h in hits[:5]:
        if h["mixer"]:
            reasons.append(
                f"{h['hop']}-hop counterparty {_short(h['address'])} is a "
                f"sanctioned mixer ({h['name']}, {h['program']}) — funds "
                "touched a listed mixer."
            )
        else:
            reasons.append(
                f"{h['hop']}-hop counterparty {_short(h['address'])} is on "
                f"the OFAC SDN list as {h['name']} ({h['program']})."
            )
    if len(hits) > 5:
        reasons.append(
            f"Plus {len(hits) - 5} more sanctioned counterparties in the "
            "walk (showing 5)."
        )
    if not reasons:
        reasons.append("No OFAC SDN match for this address.")
        reasons.append(
            f"No sanctioned counterparties among the "
            f"{counterparties_checked} counterparties checked "
            f"({hops_checked} hop{'s' if hops_checked != 1 else ''}, "
            f"{txs_scanned} transactions scanned)."
        )
    if walk_skipped:
        reasons.append(
            "Funder walk skipped — the direct SDN match already sets the "
            "maximum score."
        )
    if walk_partial:
        reasons.append(
            "Note: part of the funder walk timed out upstream, so the hop "
            "check is partial — a low score here is weaker evidence than "
            "a complete walk."
        )

    return {
        "address": address,
        "chain_family": family,
        "score": score,
        "level": level,
        "reasons": reasons,
        "direct_hit": direct,
        "nearest_hop": min((h["hop"] for h in hits), default=None),
        "hits": hits[:5],
        "hits_total": len(hits),
        "coverage": {
            "hops_checked": hops_checked,
            "max_hop": 1 if family == "solana" else 3,
            "counterparties_checked": counterparties_checked,
            "txs_scanned": txs_scanned,
            "limited_to_loaded_txs": True,
            "walk_partial": walk_partial,
            "walk_skipped": walk_skipped,
            "note": COVERAGE_NOTES[family],
        },
        "family_note": FAMILY_NOTES[family],
        "sdn": {
            "source": OFAC_SDN_SOURCE,
            "date": OFAC_SDN_DATE,
            "labels_total": OFAC_SDN_COUNT,
            "labels_for_family": FAMILY_COUNTS[family],
        },
        "method": METHOD_NOTE,
    }


# ---------------------------------------------------------------- walkers
# Each walker runs the funder walk over the transactions the address page
# already loads, with a hard cap on upstream fetches. Imports of the
# chain modules are lazy (inside the function) so this module never
# creates an import cycle with explorer / bitcoin / solana_explorer.

_WALK_TIMEOUT_S = 25


def _evm_counterparties(items, self_addr):
    self_l = self_addr.lower()
    out = set()
    for t in items or []:
        for side in (t.get("from"), t.get("to")):
            h = (side or {}).get("hash") if isinstance(side, dict) else None
            if h and h.lower() != self_l:
                out.add(h.lower())
    return out


async def _evm_txs_many(chain, addrs):
    """One tx page per address; (pages, partial) — partial on timeout."""
    from common.explorer import _address_txs

    async def one(a):
        try:
            d = await _address_txs(chain, a, {})
            return d.get("items", []) if isinstance(d, dict) else []
        except Exception:
            return []

    if not addrs:
        return [], False
    try:
        pages = await asyncio.wait_for(
            asyncio.gather(*[one(a) for a in addrs]), timeout=_WALK_TIMEOUT_S
        )
        return pages, False
    except (asyncio.TimeoutError, Exception):
        return [[] for _ in addrs], True


async def _resolve_evm_name(name):
    """Resolve an ENS name (or other non-hex input) to an EVM address via
    Blockscout /search on Ethereum, mirroring explorer._search's item
    parsing: `address` and `ens_domain` hits both carry `address_hash`.
    Returns the address, or None when no address hit is found. An
    upstream failure propagates as UpstreamError (honest 502), it is
    never reported as an invalid address."""
    from common.clients import bs

    d = await bs("ethereum", "/search", params={"q": name})
    if not isinstance(d, dict):
        return None
    for it in d.get("items", []) or []:
        if it.get("type") in ("address", "ens_domain"):
            h = it.get("address_hash")
            if h and re.fullmatch(r"0x[0-9a-fA-F]{40}", h):
                return h
    return None


async def evm_risk(chain, addr):
    """Risk for one EVM address on `chain`. 3-hop walk, budgets 10/5."""
    from common.clients import UpstreamError, _cache_get, _cache_set

    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", addr or ""):
        resolved = await _resolve_evm_name(addr or "")
        if resolved is None:
            raise UpstreamError(
                "Invalid EVM address (expected 0x + 40 hex chars)", status=400
            )
        addr = resolved
    cache_key = f"risk:evm:{chain}:{addr.lower()}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    direct = lookup(addr)
    hops, txs_scanned, partial = {}, 0, False
    if direct is None:
        from common.explorer import _address_txs

        page = await _address_txs(chain, addr, {})
        if not isinstance(page, dict):
            raise UpstreamError("Address not found", status=404)
        items = page.get("items", [])
        txs_scanned = len(items)
        hop1 = _evm_counterparties(items, addr)
        hops[1] = sorted(hop1)
        seen = set(hop1)
        pages, p2 = await _evm_txs_many(chain, hops[1][:10])
        partial = partial or p2
        hop2 = set()
        for its in pages:
            txs_scanned += len(its)
            hop2 |= _evm_counterparties(its, addr)
        hop2 -= seen
        hop2.discard(addr.lower())
        hops[2] = sorted(hop2)
        seen |= hop2
        pages, p3 = await _evm_txs_many(chain, hops[2][:5])
        partial = partial or p3
        hop3 = set()
        for its in pages:
            txs_scanned += len(its)
            hop3 |= _evm_counterparties(its, addr)
        hop3 -= seen
        hop3.discard(addr.lower())
        hops[3] = sorted(hop3)
    out = assess(
        addr, "evm", hops,
        txs_scanned=txs_scanned,
        hops_checked=0 if direct is not None else 3,
        counterparties_checked=sum(len(v) for v in hops.values()),
        walk_partial=partial,
        walk_skipped=direct is not None,
    )
    _cache_set(cache_key, out, ttl=600)
    return out


async def _btc_cps_many(addrs):
    """One counterparty page per address; (results, partial)."""
    from common.bitcoin import btc_counterparties

    async def one(a):
        try:
            return await btc_counterparties(a)
        except Exception:
            return {"counterparties": [], "txs_scanned": 0}

    if not addrs:
        return [], False
    try:
        res = await asyncio.wait_for(
            asyncio.gather(*[one(a) for a in addrs]), timeout=_WALK_TIMEOUT_S
        )
        return res, False
    except (asyncio.TimeoutError, Exception):
        return [{"counterparties": [], "txs_scanned": 0} for _ in addrs], True


async def bitcoin_risk(addr):
    """Risk for one Bitcoin address. 3-hop walk, budgets 10/5."""
    from common.bitcoin import btc_counterparties, is_bitcoin_address
    from common.clients import UpstreamError, _cache_get, _cache_set

    if not is_bitcoin_address(addr or ""):
        raise UpstreamError("Invalid Bitcoin address", status=400)
    cache_key = f"risk:btc:{addr}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    direct = lookup(addr)
    hops, txs_scanned, partial = {}, 0, False
    if direct is None:
        first = await btc_counterparties(addr)
        txs_scanned = first["txs_scanned"]
        hops[1] = sorted(set(first["counterparties"]))
        seen = set(hops[1])
        res, p2 = await _btc_cps_many(hops[1][:10])
        partial = partial or p2
        hop2 = set()
        for r in res:
            txs_scanned += r["txs_scanned"]
            hop2 |= set(r["counterparties"])
        hop2 -= seen
        hop2.discard(addr)
        hops[2] = sorted(hop2)
        seen |= hop2
        res, p3 = await _btc_cps_many(hops[2][:5])
        partial = partial or p3
        hop3 = set()
        for r in res:
            txs_scanned += r["txs_scanned"]
            hop3 |= set(r["counterparties"])
        hop3 -= seen
        hop3.discard(addr)
        hops[3] = sorted(hop3)
    out = assess(
        addr, "bitcoin", hops,
        txs_scanned=txs_scanned,
        hops_checked=0 if direct is not None else 3,
        counterparties_checked=sum(len(v) for v in hops.values()),
        walk_partial=partial,
        walk_skipped=direct is not None,
    )
    _cache_set(cache_key, out, ttl=600)
    return out


async def solana_risk(addr):
    """Risk for one Solana address. 1-hop walk only (see module docs)."""
    from common.clients import UpstreamError, _cache_get, _cache_set
    from common.solana import is_solana_address

    if not is_solana_address(addr or ""):
        raise UpstreamError("Invalid Solana address", status=400)
    cache_key = f"risk:sol:{addr}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    direct = lookup(addr)
    hops, txs_scanned, partial = {}, 0, False
    if direct is None:
        from common.solana_explorer import _signatures, sol_tx

        sigs = await _signatures(addr, limit=25)
        items = sigs.get("items", []) if isinstance(sigs, dict) else []
        sig_list = [
            s.get("signature") for s in items[:10] if s.get("signature")
        ]

        async def one(sig):
            try:
                return await sol_tx(sig)
            except Exception:
                return None

        try:
            txs = await asyncio.wait_for(
                asyncio.gather(*[one(s) for s in sig_list]),
                timeout=_WALK_TIMEOUT_S,
            )
        except (asyncio.TimeoutError, Exception):
            txs, partial = [None] * len(sig_list), True
        cps = set()
        for tx in txs:
            if not tx:
                continue
            txs_scanned += 1
            for a in tx.get("accounts") or []:
                adr = a.get("address") if isinstance(a, dict) else None
                if adr and adr != addr and adr not in KNOWN_PROGRAMS:
                    cps.add(adr)
        hops[1] = sorted(cps)
    out = assess(
        addr, "solana", hops,
        txs_scanned=txs_scanned,
        hops_checked=0 if direct is not None else 1,
        counterparties_checked=sum(len(v) for v in hops.values()),
        walk_partial=partial,
        walk_skipped=direct is not None,
    )
    _cache_set(cache_key, out, ttl=600)
    return out
