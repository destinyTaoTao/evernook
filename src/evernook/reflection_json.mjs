// A generation stopping check, never a JSON repair or citation validator.
export function completeObject(text) {
  try {
    const value = JSON.parse(text);
    return value !== null && typeof value === 'object' && !Array.isArray(value);
  } catch { return false; }
}
