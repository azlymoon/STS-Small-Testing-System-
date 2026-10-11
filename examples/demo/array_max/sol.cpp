#include <iostream>
int main() {
    int n; std::cin >> n;
    long long best, x; std::cin >> best;
    for (int i = 1; i < n; i++) { std::cin >> x; if (x > best) best = x; }
    std::cout << best << "\n";
}
