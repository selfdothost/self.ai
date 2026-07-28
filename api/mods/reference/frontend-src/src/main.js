// The bundle entry. Importing the custom-element-compiled component is a side
// effect: it registers <mod-reference> via customElements.define() at module
// evaluation, so a dynamic import() of this bundle (self.chat's R2 loader) is
// all it takes to make the tag available. Nothing is exported --- the host
// only ever touches the DOM element, never the component's internals (the
// custom-element choice that sidesteps sveltejs/svelte#13186).
import './ReferenceStatus.svelte';
