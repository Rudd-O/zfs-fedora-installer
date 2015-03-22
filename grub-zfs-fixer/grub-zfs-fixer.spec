Name:           grub-zfs-fixer
Version:        0.0.2
Release:        1
Summary:        Fixes GRUB2 grub-mkconfig

Group:          System Environment/Kernel
License:        GPLv2+
URL:            http://github.com/Rudd-O/zfs-fedora-installer
Source0:        grub-zfs-fixer.tar.gz
BuildRoot:      %{_tmppath}/%{name}-%{version}-%{release}-root-%(%{__id_u} -n)
Requires:       python, gawk, coreutils, grub2-tools
BuildArch:      noarch

%description
This package patches grub2-mkconfig to ensure that a proper
GRUB2 configuration will be built.

%prep
%setup -q -n %{name}

%install
%{__rm} -rf $RPM_BUILD_ROOT
mkdir -p %{?buildroot}/%{_sbindir}
install fix-grub-mkconfig %{?buildroot}/%{_sbindir}

%files
%{_sbindir}/*

%post
if [ -f %{_sbindir}/grub2-mkconfig ] ; then
    /usr/sbin/fix-grub-mkconfig
fi

%triggerin -- grub2-tools
%{_sbindir}/fix-grub-mkconfig

%triggerun -- grub2-tools
%{_bindir}/rm -f %{_sbindir}/grub2-mkconfig.bak

%changelog
* Sun Mar 22 2015 Manuel Amador (Rudd-O) <rudd-o@rudd-o.com> - 0.0.1-1
- First conceived.
