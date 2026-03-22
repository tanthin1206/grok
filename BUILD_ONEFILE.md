# Build 1 file EXE (Windows)

## Muc tieu
- Dong goi app Grok thanh **1 file** `RUN_GROK.exe`.
- Entrypoint build: `Run_grok.py`.
- Du lieu runtime (`data_general`, `Workflows`, `chrome_user_data`, `chrome_user_data_grok`, `downloads`) **khong** nhung vao EXE.
- Khi EXE chay lan dau, app tu tao cac thu muc can thiet ben canh EXE.

## Files build chinh
- `grok_onefile.spec`: cau hinh PyInstaller onefile cho ban Grok.
- `build_onefile.ps1`: script build chinh.
- `build_onefile.bat`: wrapper de chay nhanh tren Windows.
- `requirements.txt`: dependencies runtime de may khac co the chay source neu can.
- `requirements-build.txt`: dependencies cho build tool.

## Cach build
1. Mo PowerShell tai thu muc project `D:\veo 3`.
2. Chay:

```powershell
.\build_onefile.ps1 -InstallDeps
```

Hoac dung file bat:

```bat
build_onefile.bat -InstallDeps
```

3. Ket qua:
- EXE tai: `dist\RUN_GROK.exe`

## Cach gui cho nguoi khac dung
- Uu tien gui `dist\RUN_GROK.exe` thay vi gui source code.
- Nguoi dung chi can mo file `.exe`, khong mo `Run_grok.py` bang trinh duyet hay editor.
- Neu gui source, nguoi nhan can cai Python va chay:

```powershell
py -3 Run_grok.py
```

Hoac:

```powershell
python Run_grok.py
```

## Luu y khi phat hanh
- Lan mo dau tien cua onefile co the cham hon vi phai giai nen tam.
- Windows SmartScreen/Defender co the canh bao voi file EXE moi build chua ky so.
- Neu chay tren may khac, dam bao may co Chrome de tinh nang GROK/CDP hoat dong.
- Neu chay bang source, cai dep bang `pip install -r requirements.txt` truoc.

## Test checklist
1. Chay `dist\RUN_GROK.exe` tren may khong cai Python.
2. Xac nhan app mo len duoc.
3. Xac nhan app tao cac thu muc runtime can thiet sau lan mo dau.
4. Thu mo tab GROK va nut mo Chrome dang nhap.
5. Thu tinh nang can ffmpeg (noi/cat video).
6. Dong/mo lai app, xac nhan cau hinh van duoc luu va doc lai.

## Build lai nhanh
```powershell
.\build_onefile.ps1
```

## Ghi chu ky thuat
- `grok_onefile.spec` da bao gom hidden imports cho Playwright/PyQt6 va cac module import dong.
- `merge+video.py` duoc them vao bundle vi duoc nap bang file path (importlib spec_from_file_location).
- Khong include profile/data runtime hien tai vao EXE de tranh phinh file va loi du lieu cu.
