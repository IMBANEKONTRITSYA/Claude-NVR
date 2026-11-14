import { describe, it, expect } from "vitest";
import {
  ALL_LOCATIONS,
  NO_LOCATION,
  filterCameras,
  groupKey,
  locationGroups,
} from "./cameraGroups";

const cam = (id: number, name: string, location: string | null) => ({ id, name, location });

describe("ключ группы", () => {
  it("схлопывает пустую, пробельную и отсутствующую локацию в одну группу", () => {
    // Иначе парк из 250 камер разложился бы на три одинаковых с виду
    // группы «без локации» — по одной на каждое написание пустоты.
    expect(groupKey("")).toBe(NO_LOCATION);
    expect(groupKey("   ")).toBe(NO_LOCATION);
    expect(groupKey(null)).toBe(NO_LOCATION);
    expect(groupKey(undefined)).toBe(NO_LOCATION);
  });

  it("не трогает заполненную локацию, кроме краевых пробелов", () => {
    expect(groupKey(" Корпус 3 ")).toBe("Корпус 3");
  });
});

describe("группы локаций", () => {
  it("считают камеры в каждой группе", () => {
    const groups = locationGroups([
      cam(1, "К1", "Корпус 1"), cam(2, "К2", "Корпус 1"), cam(3, "К3", "Склад"),
    ]);
    expect(groups).toEqual([
      { location: "Корпус 1", count: 2 },
      { location: "Склад", count: 1 },
    ]);
  });

  it("сортируют по-человечески: «Корпус 10» после «Корпус 9», а не после «Корпус 1»", () => {
    const groups = locationGroups([
      cam(1, "a", "Корпус 10"), cam(2, "b", "Корпус 2"), cam(3, "c", "Корпус 1"),
    ]);
    expect(groups.map(g => g.location)).toEqual(["Корпус 1", "Корпус 2", "Корпус 10"]);
  });

  it("держат группу «без локации» последней", () => {
    // Это не место на объекте, а признак незаполненной карточки: в
    // середине раскладки по корпусам она читалась бы как ещё один корпус.
    const groups = locationGroups([
      cam(1, "a", null), cam(2, "b", "Я-корпус"), cam(3, "c", "Ангар"),
    ]);
    expect(groups.map(g => g.location)).toEqual(["Ангар", "Я-корпус", NO_LOCATION]);
  });

  it("на пустом парке не выдумывают групп", () => {
    expect(locationGroups([])).toEqual([]);
  });
});

describe("отбор камер", () => {
  const park = [
    cam(1, "Проходная", "Корпус 1"),
    cam(2, "Склад северный", "Склад"),
    cam(3, "Ворота", null),
  ];

  it("без группы и без текста отдаёт весь парк в исходном порядке", () => {
    expect(filterCameras(park, ALL_LOCATIONS, "").map(c => c.id)).toEqual([1, 2, 3]);
  });

  it("отбирает по группе, включая группу «без локации»", () => {
    expect(filterCameras(park, "Склад", "").map(c => c.id)).toEqual([2]);
    expect(filterCameras(park, NO_LOCATION, "").map(c => c.id)).toEqual([3]);
  });

  it("применяет группу и текст ВМЕСТЕ, а не по очереди", () => {
    // Класс ошибки, ради которого проверка и написана: реализация, где
    // текст отменяет выбранную группу, на этом наборе вернула бы обе
    // «складские» строки, а оператор ждёт одну — из выбранной группы.
    expect(filterCameras(park, "Склад", "склад").map(c => c.id)).toEqual([2]);
    expect(filterCameras(park, "Корпус 1", "склад")).toEqual([]);
  });

  it("ищет по id, имени и локации без учёта регистра", () => {
    expect(filterCameras(park, ALL_LOCATIONS, "ПРОХОДНАЯ").map(c => c.id)).toEqual([1]);
    expect(filterCameras(park, ALL_LOCATIONS, "корпус").map(c => c.id)).toEqual([1]);
    expect(filterCameras(park, ALL_LOCATIONS, "3").map(c => c.id)).toEqual([3]);
  });
});
