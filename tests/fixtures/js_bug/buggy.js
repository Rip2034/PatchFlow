// Buggy JavaScript — ReferenceError: undefined variable
function calculateRatio(numerator, denominator) {
    if (denominator === 0) {
        return 0;
    }
    return numerator / denom;  // BUG: 'denom' is undefined, should be 'denominator'
}

function main() {
    const result = calculateRatio(10, 2);
    console.log(`Result: ${result}`);
}

main();
