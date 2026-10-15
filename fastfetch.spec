Name:           fastfetch
Version:        2.69.0
Release:        0%{?dist}
Summary:        A maintained, feature-rich and faster neofetch-like tool
License:        MIT
URL:            https://github.com/satellaos-official/

AutoReq: no

%description
Fastfetch is a neofetch-like tool for fetching system information and displaying them in a pretty way.

%prep
%build

%install
rm -rf %{buildroot}
mkdir -p %{buildroot}%{_bindir}
cp -p %{_sourcedir}/fastfetch %{buildroot}%{_bindir}/
cp -p %{_sourcedir}/flashfetch %{buildroot}%{_bindir}/

%files
%{_bindir}/fastfetch
%{_bindir}/flashfetch
