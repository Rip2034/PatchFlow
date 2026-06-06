// Buggy TypeScript — type error: passing string to number param
function calculateRatio(numerator: number, denominator: number): number {
    if (denominator === 0) {
        return 0;
    }
    return numerator / denominator;
}

function main(): void {
    const result = calculateRatio("10", 2);  // BUG: "10" is string, should be number
    console.log(`Result: ${result}`);
}

main();
