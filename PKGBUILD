pkgname=fastfetch
pkgver=2.69.0
pkgrel=0
pkgdesc="A maintained, feature-rich and faster neofetch-like tool"
arch=('x86_64')
url="https://github.com/satellaos-official/"
license=('MIT')
depends=('glibc')
options=('!debug')
source=('fastfetch'
        'flashfetch')
sha256sums=('SKIP'
            'SKIP')

package() {
    install -Dm755 "$srcdir/fastfetch"  "$pkgdir/usr/bin/fastfetch"
    install -Dm755 "$srcdir/flashfetch" "$pkgdir/usr/bin/flashfetch"
}
