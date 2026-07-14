use unialloc::{
    lifetime_placement_class, AllocationMetadata, LifetimePlacementClass, LIFETIME_HINT_EPHEMERAL,
    LIFETIME_HINT_LONG_LIVED,
};

#[test]
fn prototype_lifetime_classes_are_explicit_and_conservative() {
    assert_eq!(
        lifetime_placement_class(LIFETIME_HINT_EPHEMERAL),
        LifetimePlacementClass::Ephemeral
    );
    assert_eq!(
        lifetime_placement_class(LIFETIME_HINT_LONG_LIVED),
        LifetimePlacementClass::LongLived
    );
    assert_eq!(
        lifetime_placement_class(AllocationMetadata::unknown().lifetime_hint),
        LifetimePlacementClass::Unknown
    );
    assert_eq!(
        lifetime_placement_class(0x11),
        LifetimePlacementClass::Unknown,
        "legacy/custom hint values keep their identity without acquiring policy semantics"
    );
}
